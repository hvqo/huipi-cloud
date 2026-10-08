"""Safe parsing task status endpoint."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage
from huipi_cloud.modules.assignments.schemas import ErrorResponse
from huipi_cloud.modules.parsing import service
from huipi_cloud.modules.parsing.schemas import ParsedDocumentRead, ParsingTaskStatusRead

router = APIRouter(prefix="/submissions", tags=["parsing"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get(
    "/{submission_id}/parsing-task",
    response_model=ParsingTaskStatusRead,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "提交或任务不存在"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "数据库暂时不可用",
        },
    },
)
async def get_parsing_task_status(
    submission_id: UUID,
    session: DbSession,
) -> ParsingTaskStatusRead:
    """Return task progress and safe error summary without lease credentials."""
    return await service.get_status(session, submission_id)


@router.get(
    "/{submission_id}/parsed-document",
    response_model=ParsedDocumentRead,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "提交不存在"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "数据库暂时不可用",
        },
    },
)
async def get_parsed_document(
    submission_id: UUID,
    session: DbSession,
) -> ParsedDocumentRead:
    """Return parsing status and indexed metadata without exposing S3 keys."""
    return await service.get_parsed_document(session, submission_id)


@router.get(
    "/{submission_id}/parsed-document/markdown",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "提交不存在"},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse, "description": "解析尚未完成"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ErrorResponse,
            "description": "对象存储或数据库暂时不可用",
        },
    },
)
async def get_parsed_markdown(
    submission_id: UUID,
    session: DbSession,
    storage: Annotated[S3ObjectStorage, Depends(get_object_storage)],
) -> StreamingResponse:
    """Stream the complete Markdown output from private object storage."""
    artifact = await service.get_parsed_artifact_for_markdown(session, submission_id)
    downloaded = await storage.download(artifact.markdown_key)
    return StreamingResponse(
        downloaded.chunks(),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="submission-{submission_id}.md"',
            "Content-Length": str(artifact.markdown_size_bytes),
        },
    )
