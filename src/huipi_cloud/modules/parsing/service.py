"""Parsing task query use cases and retry timing policy."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.modules.parsing import repository
from huipi_cloud.modules.parsing.enums import ParsingTaskStatus
from huipi_cloud.modules.parsing.errors import ParsingResultNotReadyError
from huipi_cloud.modules.parsing.models import ParsedArtifact
from huipi_cloud.modules.parsing.schemas import (
    ParsedArtifactSummary,
    ParsedDocumentRead,
    ParsingTaskStatusRead,
)
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


async def get_parsed_document(
    session: AsyncSession,
    submission_id: UUID,
) -> ParsedDocumentRead:
    """Return task progress and safe result metadata for one submission."""
    result = await repository.get_task_with_artifact_by_submission(session, submission_id)
    if result is None:
        raise SubmissionNotFoundError("提交记录或解析任务不存在")
    task, artifact = result
    if task.status == ParsingTaskStatus.SUCCEEDED.value and artifact is None:
        raise RuntimeError("succeeded parsing task has no parsed artifact index")
    return ParsedDocumentRead(
        submission_id=submission_id,
        status=task.status,
        parsed_document=(
            ParsedArtifactSummary.model_validate(artifact) if artifact is not None else None
        ),
    )


async def get_parsed_artifact_for_markdown(
    session: AsyncSession,
    submission_id: UUID,
) -> ParsedArtifact:
    """Return only an indexed artifact for a successful task."""
    result = await repository.get_task_with_artifact_by_submission(session, submission_id)
    if result is None:
        raise SubmissionNotFoundError("提交记录或解析任务不存在")
    task, artifact = result
    if task.status != ParsingTaskStatus.SUCCEEDED.value or artifact is None:
        raise ParsingResultNotReadyError(task.status)
    return artifact
