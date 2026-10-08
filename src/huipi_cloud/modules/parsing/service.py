"""Parsing task query use cases and retry timing policy."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.modules.parsing import repository
from huipi_cloud.modules.parsing.schemas import ParsingTaskStatusRead
from huipi_cloud.modules.submissions.errors import SubmissionNotFoundError


def retry_delay_seconds(attempt_count: int, base_seconds: int, max_seconds: int) -> int:
    """Return bounded exponential backoff for the attempt that just failed."""
    if attempt_count < 1 or base_seconds < 1 or max_seconds < 1:
        raise ValueError("retry timing values must be positive")
    return min(base_seconds * (2 ** (attempt_count - 1)), max_seconds)


async def get_status(
    session: AsyncSession,
    submission_id: UUID,
) -> ParsingTaskStatusRead:
    task = await repository.get_task_by_submission(session, submission_id)
    if task is None:
        raise SubmissionNotFoundError("提交记录或解析任务不存在")
    return ParsingTaskStatusRead.model_validate(task)
