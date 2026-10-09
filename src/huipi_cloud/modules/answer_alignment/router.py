"""Read-only API for versioned answer-alignment results."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage
from huipi_cloud.modules.answer_alignment import service
from huipi_cloud.modules.answer_alignment.protocol import (
    AlignmentErrorResponse,
    AlignmentSummaryRead,
    QuestionAlignmentRead,
)

router = APIRouter(prefix="/submissions", tags=["answer-alignment"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
Storage = Annotated[S3ObjectStorage, Depends(get_object_storage)]
ALIGNMENT_ERRORS = {
    status.HTTP_404_NOT_FOUND: {
        "model": AlignmentErrorResponse,
        "description": "提交记录或题目不存在",
    },
    status.HTTP_409_CONFLICT: {
        "model": AlignmentErrorResponse,
        "description": "规范化或答案对齐结果尚不可用",
    },
    status.HTTP_413_CONTENT_TOO_LARGE: {
        "model": AlignmentErrorResponse,
        "description": "单题答案来源超过API响应上限",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": AlignmentErrorResponse,
        "description": "数据库、对象存储或索引产物暂时不可用",
    },
}


@router.get(
    "/{submission_id}/answer-alignment",
    response_model=AlignmentSummaryRead,
    responses=ALIGNMENT_ERRORS,
)
async def get_answer_alignment_summary(
    submission_id: UUID,
    session: DbSession,
    storage: Storage,
) -> AlignmentSummaryRead:
    """Return alignment status and counts without object keys or answer keys."""

    return await service.get_summary(session, submission_id, storage=storage)


@router.get(
    "/{submission_id}/answer-alignment/questions/{question_id}",
    response_model=QuestionAlignmentRead,
    responses=ALIGNMENT_ERRORS,
)
async def get_question_answer_alignment(
    submission_id: UUID,
    question_id: UUID,
    session: DbSession,
    storage: Storage,
) -> QuestionAlignmentRead:
    """Return one question's candidate and trace without standard-answer data."""

    return await service.get_question_alignment(
        session,
        storage,
        submission_id,
        question_id,
    )
