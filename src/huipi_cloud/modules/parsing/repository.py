"""PostgreSQL persistence operations for parsing task claims and state changes."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, case, func, literal, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.modules.parsing.enums import ParsingTaskStatus, can_transition
from huipi_cloud.modules.parsing.errors import FailureSummary
from huipi_cloud.modules.parsing.executor import ParsingInput
from huipi_cloud.modules.submissions.models import ParsingTask, SubmissionFile

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class ClaimedTask:
    task: ParsingInput
    lease_token: UUID
    lease_expires_at: datetime


async def claim_next_task(
    session_factory: SessionFactory,
    *,
    lease_seconds: int,
    now: datetime | None = None,
) -> ClaimedTask | None:
    """Claim the oldest eligible task with SKIP LOCKED in a short transaction."""
    async with session_factory() as session, session.begin():
        claimed_at = await _operation_time(session, now)
        await _fail_exhausted_expired_leases(session, claimed_at)
        eligible = or_(
            and_(
                ParsingTask.status == ParsingTaskStatus.PENDING.value,
                ParsingTask.attempt_count < ParsingTask.max_attempts,
            ),
            and_(
                ParsingTask.status == ParsingTaskStatus.RETRY_WAIT.value,
                ParsingTask.next_run_at <= claimed_at,
                ParsingTask.attempt_count < ParsingTask.max_attempts,
            ),
            and_(
                ParsingTask.status == ParsingTaskStatus.RUNNING.value,
                ParsingTask.lease_expires_at <= claimed_at,
                ParsingTask.attempt_count < ParsingTask.max_attempts,
            ),
        )
        row = (
            await session.execute(
                select(ParsingTask, SubmissionFile)
                .join(SubmissionFile, SubmissionFile.submission_id == ParsingTask.submission_id)
                .where(eligible)
                .order_by(ParsingTask.created_at, ParsingTask.id)
                .limit(1)
                .with_for_update(skip_locked=True, of=ParsingTask)
            )
        ).first()
        if row is None:
            return None

        task, submission_file = row
        previous_status = ParsingTaskStatus(task.status)
        if previous_status is not ParsingTaskStatus.RUNNING and not can_transition(
            previous_status,
            ParsingTaskStatus.RUNNING,
        ):
            return None

        lease_token = uuid4()
        task.status = ParsingTaskStatus.RUNNING.value
        task.attempt_count += 1
        task.next_run_at = None
        task.lease_token = lease_token
        if now is None:
            database_now = func.clock_timestamp()
            task.lease_expires_at = _database_time_after(lease_seconds)
            task.started_at = task.started_at or database_now
            task.updated_at = database_now
        else:
            task.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
            task.started_at = task.started_at or claimed_at
            task.updated_at = claimed_at
        task.finished_at = None
        if previous_status is ParsingTaskStatus.RUNNING:
            task.last_error_code = "lease_expired"
            task.last_error_message = "上一次执行租约到期，任务已重新领取"
        await session.flush()
        if now is None:
            await session.refresh(task, attribute_names=["lease_expires_at"])
        lease_expires_at = task.lease_expires_at
        if lease_expires_at is None:
            raise RuntimeError("PostgreSQL did not return the new task lease")
        return ClaimedTask(
            task=ParsingInput(
                task_id=task.id,
                submission_id=task.submission_id,
                bucket=submission_file.bucket,
                object_key=submission_file.object_key,
                content_type=submission_file.content_type,
                size_bytes=submission_file.size_bytes,
                sha256=submission_file.sha256,
                attempt_count=task.attempt_count,
            ),
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
        )


async def heartbeat(
    session_factory: SessionFactory,
    *,
    task_id: UUID,
    lease_token: UUID,
    lease_seconds: int,
    now: datetime | None = None,
) -> datetime | None:
    """Extend a lease only while its token is current and its lease is unexpired."""
    async with session_factory() as session, session.begin():
        checked_at = _time_expression(now)
        new_expiry = _database_time_after(lease_seconds) if now is None else (
            checked_at + timedelta(seconds=lease_seconds)
        )
        result = await session.execute(
            update(ParsingTask)
            .where(
                ParsingTask.id == task_id,
                ParsingTask.status == ParsingTaskStatus.RUNNING.value,
                ParsingTask.lease_token == lease_token,
                ParsingTask.lease_expires_at > checked_at,
            )
            .values(lease_expires_at=new_expiry, updated_at=checked_at)
            .returning(ParsingTask.lease_expires_at)
        )
        return result.scalar_one_or_none()


async def complete_task(
    session_factory: SessionFactory,
    *,
    task_id: UUID,
    lease_token: UUID,
    now: datetime | None = None,
) -> bool:
    """Finish a task only if this worker still owns a valid lease."""
    async with session_factory() as session, session.begin():
        completed_at = _time_expression(now)
        result = await session.execute(
            update(ParsingTask)
            .where(
                ParsingTask.id == task_id,
                ParsingTask.status == ParsingTaskStatus.RUNNING.value,
                ParsingTask.lease_token == lease_token,
                ParsingTask.lease_expires_at > completed_at,
            )
            .values(
                status=ParsingTaskStatus.SUCCEEDED.value,
                lease_token=None,
                lease_expires_at=None,
                next_run_at=None,
                finished_at=completed_at,
                last_error_code=None,
                last_error_message=None,
                updated_at=completed_at,
            )
            .returning(ParsingTask.id)
        )
        return result.scalar_one_or_none() is not None


async def record_failure(
    session_factory: SessionFactory,
    *,
    task_id: UUID,
    lease_token: UUID,
    failure: FailureSummary,
    retry_delay_seconds: int,
    now: datetime | None = None,
) -> ParsingTaskStatus | None:
    """Record a bounded failure summary or schedule a due-time retry."""
    async with session_factory() as session, session.begin():
        failed_at = _time_expression(now)
        retry_allowed = and_(
            literal(failure.retryable),
            ParsingTask.attempt_count < ParsingTask.max_attempts,
        )
        retry_due = (
            failed_at + timedelta(seconds=retry_delay_seconds)
            if now is not None
            else _database_time_after(retry_delay_seconds)
        )
        result = await session.execute(
            update(ParsingTask)
            .where(
                ParsingTask.id == task_id,
                ParsingTask.status == ParsingTaskStatus.RUNNING.value,
                ParsingTask.lease_token == lease_token,
                ParsingTask.lease_expires_at > failed_at,
            )
            .values(
                status=case(
                    (retry_allowed, ParsingTaskStatus.RETRY_WAIT.value),
                    else_=ParsingTaskStatus.FAILED.value,
                ),
                next_run_at=case((retry_allowed, retry_due), else_=None),
                lease_token=None,
                lease_expires_at=None,
                finished_at=case((retry_allowed, None), else_=failed_at),
                last_error_code=failure.code[:64],
                last_error_message=failure.message[:240],
                updated_at=failed_at,
            )
            .returning(ParsingTask.status)
        )
        status_value = result.scalar_one_or_none()
        if status_value is None:
            return None
        return ParsingTaskStatus(status_value)


async def get_task_by_submission(
    session: AsyncSession,
    submission_id: UUID,
) -> ParsingTask | None:
    """Read one task for the safe submission status endpoint."""
    return await session.scalar(
        select(ParsingTask).where(ParsingTask.submission_id == submission_id)
    )


async def _fail_exhausted_expired_leases(session: AsyncSession, now: datetime) -> None:
    expired_tasks = await session.scalars(
        select(ParsingTask)
        .where(
            ParsingTask.status == ParsingTaskStatus.RUNNING.value,
            ParsingTask.lease_expires_at <= now,
            ParsingTask.attempt_count >= ParsingTask.max_attempts,
        )
        .order_by(ParsingTask.lease_expires_at, ParsingTask.id)
        .limit(100)
        .with_for_update(skip_locked=True)
    )
    for task in expired_tasks:
        task.status = ParsingTaskStatus.FAILED.value
        task.lease_token = None
        task.lease_expires_at = None
        task.next_run_at = None
        task.finished_at = now
        task.last_error_code = "lease_expired"
        task.last_error_message = "任务租约到期，已达到最大尝试次数"
        task.updated_at = now


async def _operation_time(session: AsyncSession, value: datetime | None) -> datetime:
    """Use PostgreSQL wall time by default so Worker host clock skew cannot move leases."""
    if value is None:
        value = await session.scalar(select(func.clock_timestamp()))
        if value is None:
            raise RuntimeError("PostgreSQL did not return its current time")
    return _normalize_time(value)


def _time_expression(value: datetime | None):
    """Use PostgreSQL wall time in write predicates, after any row-lock wait."""
    if value is None:
        return func.clock_timestamp()
    return _normalize_time(value)


def _normalize_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("task runtime timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _database_time_after(seconds: int):
    """Compute a deadline from PostgreSQL wall time in the write statement."""
    if seconds <= 0:
        raise ValueError("lease duration must be positive")
    return func.clock_timestamp() + literal(seconds) * text("INTERVAL '1 second'")
