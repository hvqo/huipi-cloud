"""Student submission and private original-file endpoints."""

from collections.abc import AsyncIterator
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage
from huipi_cloud.modules.assignments.schemas import ErrorResponse
from huipi_cloud.modules.submissions import service
from huipi_cloud.modules.submissions.schemas import SubmissionRead

assignment_submission_router = APIRouter(prefix="/assignments", tags=["submissions"])
submission_router = APIRouter(prefix="/submissions", tags=["submissions"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
Storage = Annotated[S3ObjectStorage, Depends(get_object_storage)]
SUBMISSION_ERRORS = {
    400: {"model": ErrorResponse, "description": "上传内容无效"},
    404: {"model": ErrorResponse, "description": "作业或提交记录不存在"},
    409: {"model": ErrorResponse, "description": "作业状态或提交记录冲突"},
    413: {"model": ErrorResponse, "description": "文件超过配置大小上限"},
    415: {"model": ErrorResponse, "description": "文件类型不受支持"},
    500: {"model": ErrorResponse, "description": "数据库操作失败"},
    503: {"model": ErrorResponse, "description": "数据库或对象存储暂时不可用"},
}


@assignment_submission_router.post(
    "/{assignment_id}/submissions",
    response_model=SubmissionRead,
    status_code=status.HTTP_201_CREATED,
    responses=SUBMISSION_ERRORS,
)
async def create_submission(
    assignment_id: UUID,
    session: DbSession,
    storage: Storage,
    student_ref: Annotated[
        str,
        Form(
            min_length=2,
            max_length=64,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,63}$",
            description="仅供本地开发使用的模拟学生标识，不是认证身份",
        ),
    ],
    file: Annotated[UploadFile, File(description="PDF、JPG/JPEG 或 PNG 文件")],
) -> SubmissionRead:
    return await service.create_submission(
        session,
        storage,
        assignment_id=assignment_id,
        student_ref=student_ref,
        upload=file,
        max_size_bytes=settings.max_upload_size_bytes,
        max_parsing_attempts=settings.parsing_max_attempts,
    )


@assignment_submission_router.get(
    "/{assignment_id}/submissions",
    response_model=list[SubmissionRead],
    responses=SUBMISSION_ERRORS,
)
async def list_submissions(
    assignment_id: UUID,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SubmissionRead]:
    return await service.list_submissions(
        session,
        assignment_id,
        limit=limit,
        offset=offset,
    )


@submission_router.get(
    "/{submission_id}",
    response_model=SubmissionRead,
    responses=SUBMISSION_ERRORS,
)
async def get_submission(submission_id: UUID, session: DbSession) -> SubmissionRead:
    return await service.get_submission(session, submission_id)


@submission_router.get(
    "/{submission_id}/file",
    responses={
        404: {"model": ErrorResponse, "description": "提交文件不存在"},
        500: {"model": ErrorResponse, "description": "数据库操作失败"},
        503: {"model": ErrorResponse, "description": "对象存储暂时不可用"},
    },
)
async def get_submission_file(
    submission_id: UUID,
    session: DbSession,
    storage: Storage,
) -> StreamingResponse:
    submission = await service.get_submission(session, submission_id)
    stored_file = submission.file
    object_key = stored_file.object_key
    original_filename = stored_file.original_filename
    content_type = stored_file.content_type
    size_bytes = stored_file.size_bytes
    await session.rollback()
    original_object = await storage.download(object_key)

    async def stream_file() -> AsyncIterator[bytes]:
        async for chunk in original_object.chunks():
            yield chunk

    headers = {
        "Content-Disposition": _content_disposition(original_filename),
        "Content-Length": str(size_bytes),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(
        stream_file(),
        media_type=content_type,
        headers=headers,
    )


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", errors="ignore").decode("ascii")
    ascii_name = "".join(char if char.isalnum() or char in "._-" else "_" for char in ascii_name)
    if not ascii_name:
        ascii_name = "submission"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
