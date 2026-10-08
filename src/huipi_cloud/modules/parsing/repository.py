"""PostgreSQL persistence operations for parsing task claims and state changes."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
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
        lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
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
        task.lease_expires_at = lease_expires_at
        task.started_at = task.started_at or claimed_at
        task.finished_at = None
        task.updated_at = claimed_at
        if previous_status is ParsingTaskStatus.RUNNING:
            task.last_error_code = "lease_expired"
            task.last_error_message = "上一次执行租约到期，任务已重新领取"
        await session.flush()
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
        checked_at = await _operation_time(session, now)
        new_expiry = checked_at + timedelta(seconds=lease_seconds)
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
        completed_at = await _operation_time(session, now)
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
        failed_at = await _operation_time(session, now)
        task = await session.scalar(
            select(ParsingTask)
            .where(
                ParsingTask.id == task_id,
                ParsingTask.status == ParsingTaskStatus.RUNNING.value,
                ParsingTask.lease_token == lease_token,
                ParsingTask.lease_expires_at > failed_at,
            )
            .with_for_update()
        )
        if task is None:
            return None
        retry = failure.retryable and task.attempt_count < task.max_attempts
        new_status = (
            ParsingTaskStatus.RETRY_WAIT.value if retry else ParsingTaskStatus.FAILED.value
        )
        if not can_transition(ParsingTaskStatus.RUNNING, ParsingTaskStatus(new_status)):
            return None
        task.status = new_status
        task.next_run_at = (
            failed_at + timedelta(seconds=retry_delay_seconds) if retry else None
        )
        task.lease_token = None
        task.lease_expires_at = None
        task.finished_at = None if retry else failed_at
        task.last_error_code = failure.code[:64]
        task.last_error_message = failure.message[:240]
        task.updated_at = failed_at
        await session.flush()
        return ParsingTaskStatus(task.status)


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
    if value.tzinfo is None:
        raise ValueError("task runtime timestamps must be timezone-aware")
    return value.astimezone(UTC)
