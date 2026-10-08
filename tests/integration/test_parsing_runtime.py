"""Parsing task runtime tests backed by PostgreSQL row locks and constraints."""

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from huipi_cloud.core.config import Settings, settings
from huipi_cloud.infrastructure.database.base import utc_now
from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.main import app
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.parsing import repository
from huipi_cloud.modules.parsing.enums import ParsingTaskStatus, can_transition
from huipi_cloud.modules.parsing.errors import (
    PermanentParsingError,
    RetryableParsingError,
    classify_failure,
)
from huipi_cloud.modules.parsing.executor import ParsingInput
from huipi_cloud.modules.parsing.service import retry_delay_seconds
from huipi_cloud.modules.submissions.models import ParsingTask, Submission, SubmissionFile
from huipi_cloud.workers.parsing import ParsingWorker


@pytest.fixture
async def parsing_factory(
    postgres_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    async with postgres_engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE assignments CASCADE"))
    yield async_sessionmaker(postgres_engine, expire_on_commit=False)
    async with postgres_engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE assignments CASCADE"))


class SuccessfulTestParser:
    """Test-only executor; production code has no fake parser implementation."""

    def __init__(self) -> None:
        self.seen: list[ParsingInput] = []

    async def execute(self, task: ParsingInput) -> None:
        self.seen.append(task)


class WaitingTestParser:
    """Test-only executor that waits for cancellation during shutdown."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute(self, task: ParsingInput) -> None:
        del task
        self.started.set()
        try:
            await asyncio.Future()
        finally:
            self.cancelled.set()


def _runtime_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "parsing_lease_seconds": 6,
        "parsing_heartbeat_seconds": 1,
        "parsing_retry_base_seconds": 1,
        "parsing_retry_max_seconds": 8,
        "parsing_shutdown_grace_seconds": 1,
        "parsing_poll_seconds": 0.1,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def _create_task(
    factory: async_sessionmaker[AsyncSession],
    *,
    max_attempts: int = 3,
) -> tuple[UUID, UUID]:
    assignment_id = uuid4()
    submission_id = uuid4()
    task_id = uuid4()
    file_id = uuid4()
    async with factory() as session, session.begin():
        session.add(
            Assignment(
                id=assignment_id,
                title="解析任务测试",
                subject="数学",
                grade_level="高一",
                status="published",
            )
        )
        session.add(
            Submission(
                id=submission_id,
                assignment_id=assignment_id,
                student_ref=f"student-{uuid4().hex[:16]}",
                status="submitted",
            )
        )
        session.add(
            SubmissionFile(
                id=file_id,
                submission_id=submission_id,
                bucket="test-bucket",
                object_key=f"tests/{uuid4()}/answer.pdf",
                original_filename="answer.pdf",
                content_type="application/pdf",
                size_bytes=32,
                sha256="a" * 64,
            )
        )
        session.add(
            ParsingTask(
                id=task_id,
                submission_id=submission_id,
                status=ParsingTaskStatus.PENDING.value,
                max_attempts=max_attempts,
            )
        )
    return task_id, submission_id


async def _task_snapshot(
    factory: async_sessionmaker[AsyncSession],
    task_id: UUID,
) -> dict[str, object]:
    async with factory() as session:
        task = await session.get(ParsingTask, task_id)
        assert task is not None
        return {
            "status": task.status,
            "attempt_count": task.attempt_count,
            "max_attempts": task.max_attempts,
            "lease_token": task.lease_token,
            "lease_expires_at": task.lease_expires_at,
            "next_run_at": task.next_run_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "last_error_code": task.last_error_code,
            "last_error_message": task.last_error_message,
        }


@pytest.mark.anyio
async def test_pending_task_is_claimed_and_attempt_count_increases(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id, _ = await _create_task(parsing_factory)
    claimed = await repository.claim_next_task(parsing_factory, lease_seconds=30)

    assert claimed is not None
    assert claimed.task.task_id == task_id
    assert claimed.task.attempt_count == 1
    assert claimed.lease_token
    snapshot = await _task_snapshot(parsing_factory, task_id)
    assert snapshot["status"] == "running"
    assert snapshot["attempt_count"] == 1
    assert snapshot["lease_token"] == claimed.lease_token


@pytest.mark.anyio
async def test_postgres_skip_locked_allows_different_workers_to_claim_distinct_tasks(
    postgres_engine: AsyncEngine,
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_id, _ = await _create_task(parsing_factory)
    second_id, _ = await _create_task(parsing_factory)
    lock_connection = await postgres_engine.connect()
    transaction = await lock_connection.begin()
    await lock_connection.execute(
        select(ParsingTask.id)
        .where(ParsingTask.id == first_id)
        .with_for_update()
    )
    try:
        first_worker = await repository.claim_next_task(parsing_factory, lease_seconds=30)
        second_worker = await repository.claim_next_task(parsing_factory, lease_seconds=30)
        assert first_worker is not None
        assert first_worker.task.task_id == second_id
        assert second_worker is None
    finally:
        await transaction.rollback()
        await lock_connection.close()

    released_worker = await repository.claim_next_task(parsing_factory, lease_seconds=30)
    assert released_worker is not None
    assert released_worker.task.task_id == first_id
    assert released_worker.task.task_id != first_worker.task.task_id


@pytest.mark.anyio
async def test_concurrent_claims_never_return_the_same_live_task(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_task(parsing_factory)
    gate = asyncio.Barrier(3)

    async def claim_after_gate():
        await gate.wait()
        return await repository.claim_next_task(parsing_factory, lease_seconds=30)

    claims = [asyncio.create_task(claim_after_gate()) for _ in range(2)]
    await gate.wait()
    first, second = await asyncio.gather(*claims)

    assert sum(claim is not None for claim in (first, second)) == 1


@pytest.mark.anyio
async def test_parallel_workers_can_claim_different_tasks(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_task(parsing_factory)
    await _create_task(parsing_factory)
    gate = asyncio.Barrier(3)

    async def claim_after_gate():
        await gate.wait()
        return await repository.claim_next_task(parsing_factory, lease_seconds=30)

    claims = [asyncio.create_task(claim_after_gate()) for _ in range(2)]
    await gate.wait()
    first, second = await asyncio.gather(*claims)

    assert first is not None and second is not None
    assert first.task.task_id != second.task.task_id


@pytest.mark.anyio
async def test_successful_test_executor_marks_task_succeeded(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id, _ = await _create_task(parsing_factory)
    claimed = await repository.claim_next_task(parsing_factory, lease_seconds=30)
    parser = SuccessfulTestParser()
    worker = ParsingWorker(parsing_factory, parser, _runtime_settings())

    assert claimed is not None
    await worker._execute_claim(claimed)

    snapshot = await _task_snapshot(parsing_factory, task_id)
    assert snapshot["status"] == "succeeded"
    assert snapshot["finished_at"] is not None
    assert snapshot["lease_token"] is None
    assert parser.seen[0].task_id == task_id


@pytest.mark.anyio
async def test_retryable_failure_waits_until_persisted_due_time(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id, _ = await _create_task(parsing_factory)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    claimed = await repository.claim_next_task(
        parsing_factory,
        lease_seconds=60,
        now=now,
    )
    assert claimed is not None
    delay = retry_delay_seconds(claimed.task.attempt_count, 10, 100)
    result = await repository.record_failure(
        parsing_factory,
        task_id=task_id,
        lease_token=claimed.lease_token,
        failure=classify_failure(RetryableParsingError("network_timeout")),
        retry_delay_seconds=delay,
        now=now + timedelta(seconds=1),
    )

    assert result is ParsingTaskStatus.RETRY_WAIT
    snapshot = await _task_snapshot(parsing_factory, task_id)
    assert snapshot["next_run_at"] == now + timedelta(seconds=11)
    assert await repository.claim_next_task(
        parsing_factory,
        lease_seconds=60,
        now=now + timedelta(seconds=10),
    ) is None
    retry = await repository.claim_next_task(
        parsing_factory,
        lease_seconds=60,
        now=now + timedelta(seconds=11),
    )
    assert retry is not None
    assert retry.task.attempt_count == 2


@pytest.mark.anyio
async def test_non_retryable_failure_finishes_without_retry(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id, _ = await _create_task(parsing_factory)
    now = utc_now()
    claimed = await repository.claim_next_task(parsing_factory, lease_seconds=60, now=now)
    assert claimed is not None
    result = await repository.record_failure(
        parsing_factory,
        task_id=task_id,
        lease_token=claimed.lease_token,
        failure=classify_failure(PermanentParsingError("corrupt_input")),
        retry_delay_seconds=1,
        now=now + timedelta(seconds=1),
    )

    snapshot = await _task_snapshot(parsing_factory, task_id)
    assert result is ParsingTaskStatus.FAILED
    assert snapshot["status"] == "failed"
    assert snapshot["finished_at"] is not None
    assert snapshot["next_run_at"] is None
    assert snapshot["last_error_code"] == "corrupt_input"


@pytest.mark.anyio
async def test_retry_limit_marks_task_failed_on_last_attempt(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id, _ = await _create_task(parsing_factory, max_attempts=2)
    now = datetime(2026, 3, 1, tzinfo=UTC)
    first = await repository.claim_next_task(parsing_factory, lease_seconds=30, now=now)
    assert first is not None
    assert await repository.record_failure(
        parsing_factory,
        task_id=task_id,
        lease_token=first.lease_token,
        failure=classify_failure(RetryableParsingError()),
        retry_delay_seconds=5,
        now=now + timedelta(seconds=1),
    ) is ParsingTaskStatus.RETRY_WAIT
    second = await repository.claim_next_task(
        parsing_factory,
        lease_seconds=30,
        now=now + timedelta(seconds=6),
    )
    assert second is not None
    assert second.task.attempt_count == 2
    assert await repository.record_failure(
        parsing_factory,
        task_id=task_id,
        lease_token=second.lease_token,
        failure=classify_failure(RetryableParsingError()),
        retry_delay_seconds=5,
        now=now + timedelta(seconds=7),
    ) is ParsingTaskStatus.FAILED
    assert (await _task_snapshot(parsing_factory, task_id))["attempt_count"] == 2


@pytest.mark.anyio
async def test_expired_lease_is_reclaimed_with_new_token_and_old_worker_is_fenced(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id, _ = await _create_task(parsing_factory)
    now = datetime(2026, 4, 1, tzinfo=UTC)
    old = await repository.claim_next_task(parsing_factory, lease_seconds=5, now=now)
    assert old is not None
    new = await repository.claim_next_task(
        parsing_factory,
        lease_seconds=10,
        now=now + timedelta(seconds=6),
    )
    assert new is not None
    assert new.lease_token != old.lease_token
    assert new.task.attempt_count == 2
    assert not await repository.complete_task(
        parsing_factory,
        task_id=task_id,
        lease_token=old.lease_token,
        now=now + timedelta(seconds=7),
    )
    assert await repository.heartbeat(
        parsing_factory,
        task_id=task_id,
        lease_token=old.lease_token,
        lease_seconds=20,
        now=now + timedelta(seconds=7),
    ) is None
    assert (await _task_snapshot(parsing_factory, task_id))["lease_token"] == new.lease_token


@pytest.mark.anyio
async def test_heartbeat_extends_only_the_current_unexpired_lease(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id, _ = await _create_task(parsing_factory)
    now = datetime(2026, 5, 1, tzinfo=UTC)
    claimed = await repository.claim_next_task(parsing_factory, lease_seconds=5, now=now)
    assert claimed is not None
    extended = await repository.heartbeat(
        parsing_factory,
        task_id=task_id,
        lease_token=claimed.lease_token,
        lease_seconds=5,
        now=now + timedelta(seconds=2),
    )

    assert extended == now + timedelta(seconds=7)
    assert await repository.heartbeat(
        parsing_factory,
        task_id=task_id,
        lease_token=claimed.lease_token,
        lease_seconds=5,
        now=now + timedelta(seconds=8),
    ) is None


@pytest.mark.anyio
async def test_expired_lease_after_max_attempts_is_marked_failed(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id, _ = await _create_task(parsing_factory, max_attempts=1)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert await repository.claim_next_task(parsing_factory, lease_seconds=2, now=now)

    assert await repository.claim_next_task(
        parsing_factory,
        lease_seconds=2,
        now=now + timedelta(seconds=3),
    ) is None
    snapshot = await _task_snapshot(parsing_factory, task_id)
    assert snapshot["status"] == "failed"
    assert snapshot["last_error_code"] == "lease_expired"
    assert snapshot["finished_at"] == now + timedelta(seconds=3)


@pytest.mark.anyio
async def test_worker_shutdown_cancels_active_parser_and_leaves_lease_recoverable(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    task_id, _ = await _create_task(parsing_factory)
    parser = WaitingTestParser()
    config = _runtime_settings(parsing_shutdown_grace_seconds=0)
    worker = ParsingWorker(parsing_factory, parser, config)
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(stop_event))

    await asyncio.wait_for(parser.started.wait(), timeout=3)
    stop_event.set()
    await asyncio.wait_for(worker_task, timeout=3)

    assert parser.cancelled.is_set()
    snapshot = await _task_snapshot(parsing_factory, task_id)
    assert snapshot["status"] == "running"
    assert snapshot["lease_token"] is not None


@pytest.mark.anyio
async def test_invalid_state_transitions_are_rejected() -> None:
    assert can_transition(ParsingTaskStatus.PENDING, ParsingTaskStatus.RUNNING)
    assert can_transition(ParsingTaskStatus.RUNNING, ParsingTaskStatus.RETRY_WAIT)
    assert can_transition(ParsingTaskStatus.RUNNING, ParsingTaskStatus.SUCCEEDED)
    assert not can_transition(ParsingTaskStatus.PENDING, ParsingTaskStatus.SUCCEEDED)
    assert not can_transition(ParsingTaskStatus.SUCCEEDED, ParsingTaskStatus.RUNNING)
    assert not can_transition(ParsingTaskStatus.FAILED, ParsingTaskStatus.RETRY_WAIT)


@pytest.mark.anyio
async def test_duplicate_task_for_submission_is_rejected_by_postgres_unique_constraint(
    parsing_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, submission_id = await _create_task(parsing_factory)
    async with parsing_factory() as session:
        session.add(ParsingTask(submission_id=submission_id, status="pending"))
        with pytest.raises(IntegrityError) as raised:
            await session.commit()
        await session.rollback()
    assert "uq_parsing_tasks_submission_id" in str(raised.value.orig)


@pytest.mark.anyio
async def test_status_api_returns_safe_fields_and_not_lease_credentials(
    client,
    postgres_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    task_id, submission_id = await _create_task(factory)
    claimed = await repository.claim_next_task(factory, lease_seconds=30)
    assert claimed is not None
    response = await client.get(f"/api/v1/submissions/{submission_id}/parsing-task")

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == str(task_id)
    assert body["submission_id"] == str(submission_id)
    assert body["status"] == "running"
    assert "lease_token" not in body
    assert "object_key" not in body


@pytest.mark.anyio
async def test_status_api_exposes_only_the_safe_failure_summary(
    client,
    postgres_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    task_id, submission_id = await _create_task(factory)
    claimed = await repository.claim_next_task(factory, lease_seconds=30)
    assert claimed is not None
    await repository.record_failure(
        factory,
        task_id=task_id,
        lease_token=claimed.lease_token,
        failure=classify_failure(RetryableParsingError("network_timeout")),
        retry_delay_seconds=10,
    )
    response = await client.get(f"/api/v1/submissions/{submission_id}/parsing-task")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "retry_wait"
    assert body["last_error_code"] == "network_timeout"
    assert body["last_error_message"] == "解析服务暂时无响应"
    assert "lease_token" not in body
    assert "stack" not in body


@pytest.mark.anyio
async def test_status_api_returns_404_for_unknown_submission(client) -> None:
    response = await client.get(f"/api/v1/submissions/{uuid4()}/parsing-task")
    assert response.status_code == 404
    assert response.json() == {"detail": "提交记录或解析任务不存在"}


@pytest.mark.anyio
async def test_status_api_returns_safe_503_when_database_is_unavailable(client) -> None:
    async def unavailable_session():
        raise OperationalError("SELECT", {}, RuntimeError("private database endpoint"))

    previous = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = unavailable_session
    try:
        response = await client.get(f"/api/v1/submissions/{uuid4()}/parsing-task")
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db_session, None)
        else:
            app.dependency_overrides[get_db_session] = previous

    assert response.status_code == 503
    assert response.json() == {"detail": "数据库暂时不可用，请稍后重试"}
    assert "private database endpoint" not in response.text


@pytest.mark.anyio
async def test_error_classifier_never_exposes_raw_exception_text() -> None:
    failure = classify_failure(RuntimeError("secret=/private/path and token=abc123"))
    assert failure.retryable
    assert failure.code == "unexpected_error"
    assert "secret" not in failure.message
    assert "/private/path" not in failure.message
    assert len(failure.message) <= 240


@pytest.mark.anyio
async def test_backoff_is_exponential_and_bounded() -> None:
    assert retry_delay_seconds(1, 5, 18) == 5
    assert retry_delay_seconds(2, 5, 18) == 10
    assert retry_delay_seconds(3, 5, 18) == 18
    with pytest.raises(ValueError):
        retry_delay_seconds(0, 5, 18)


@pytest.mark.anyio
async def test_production_worker_refuses_to_start_without_real_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from huipi_cloud.workers import parsing_worker

    monkeypatch.setattr(settings, "parsing_executor", None)
    with pytest.raises(RuntimeError, match="No production parser is configured"):
        await parsing_worker.run_worker()


@pytest.mark.anyio
async def test_migration_created_claim_indexes_and_database_constraints_exist(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        constraints = set(
            await connection.scalars(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'parsing_tasks'::regclass"
                )
            )
        )
        indexes = set(
            await connection.scalars(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'parsing_tasks'"
                )
            )
        )

    assert "uq_parsing_tasks_submission_id" in constraints
    assert "ck_parsing_tasks_lease" in constraints
    assert "ck_parsing_tasks_status" in constraints
    assert "ix_parsing_tasks_status_next_run_created_at" in indexes
    assert "ix_parsing_tasks_status_lease_expires_at" in indexes


@pytest.mark.anyio
async def test_upgrade_preserves_legacy_submission_and_pending_task(
    migrated_test_database_url: str,
) -> None:
    """Upgrade an isolated scratch DB from P1-B and retain its pending task."""
    project_root = Path(__file__).resolve().parents[2]
    legacy_url = make_url(migrated_test_database_url)
    scratch_name = f"huipi_migration_{uuid4().hex[:12]}"
    scratch_url = legacy_url.set(database=scratch_name)
    admin_engine = create_async_engine(legacy_url.set(database="postgres"))
    scratch_engines: list[AsyncEngine] = []
    scratch_created = False

    def run_migration(revision: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision],
            cwd=project_root,
            env=os.environ
            | {"DATABASE_URL": scratch_url.render_as_string(hide_password=False)},
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise AssertionError(f"Alembic upgrade {revision} failed: {result.stderr}")

    try:
        async with admin_engine.connect() as connection:
            admin_connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await admin_connection.execute(text(f'CREATE DATABASE "{scratch_name}"'))
        scratch_created = True

        run_migration("fd1e8d651902")

        assignment_id, submission_id, task_id, file_id = uuid4(), uuid4(), uuid4(), uuid4()
        legacy_engine = create_async_engine(scratch_url)
        scratch_engines.append(legacy_engine)
        async with legacy_engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO assignments (id, title, subject, grade_level) "
                    "VALUES (:id, '旧版作业', '数学', '高一')"
                ),
                {"id": assignment_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO submissions (id, assignment_id, student_ref) "
                    "VALUES (:id, :assignment_id, 'legacy-student')"
                ),
                {"id": submission_id, "assignment_id": assignment_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO submission_files "
                    "(id, submission_id, bucket, object_key, original_filename, content_type, "
                    "size_bytes, sha256) VALUES "
                    "(:id, :submission_id, 'legacy-bucket', 'legacy/key', 'answer.pdf', "
                    "'application/pdf', 32, :sha256)"
                ),
                {"id": file_id, "submission_id": submission_id, "sha256": "b" * 64},
            )
            await connection.execute(
                text(
                    "INSERT INTO parsing_tasks (id, submission_id, status) "
                    "VALUES (:id, :submission_id, 'pending')"
                ),
                {"id": task_id, "submission_id": submission_id},
            )
        run_migration("head")

        upgraded_engine = create_async_engine(scratch_url)
        scratch_engines.append(upgraded_engine)
        async with upgraded_engine.connect() as connection:
            preserved = (
                await connection.execute(
                    text(
                        "SELECT a.id AS assignment_id, s.id AS submission_id, "
                        "p.id AS task_id, p.status, p.attempt_count, p.max_attempts, "
                        "f.id AS file_id "
                        "FROM assignments a JOIN submissions s ON s.assignment_id = a.id "
                        "JOIN parsing_tasks p ON p.submission_id = s.id "
                        "JOIN submission_files f ON f.submission_id = s.id "
                        "WHERE p.id = :task_id"
                    ),
                    {"task_id": task_id},
                )
            ).mappings().one()
        assert preserved["assignment_id"] == assignment_id
        assert preserved["submission_id"] == submission_id
        assert preserved["task_id"] == task_id
        assert preserved["file_id"] == file_id
        assert preserved["status"] == "pending"
        assert preserved["attempt_count"] == 0
        assert preserved["max_attempts"] == 3
    finally:
        for scratch_engine in scratch_engines:
            await scratch_engine.dispose()
        if scratch_created:
            async with admin_engine.connect() as connection:
                admin_connection = await connection.execution_options(
                    isolation_level="AUTOCOMMIT"
                )
                await admin_connection.execute(text(f'DROP DATABASE "{scratch_name}"'))
        await admin_engine.dispose()
