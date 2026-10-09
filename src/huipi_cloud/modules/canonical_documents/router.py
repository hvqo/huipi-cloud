"""Safe page and normalization-status API for Canonical Document v1."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage
from huipi_cloud.modules.canonical_documents import service
from huipi_cloud.modules.canonical_documents.protocol import (
    CanonicalDocumentStatusRead,
    CanonicalErrorResponse,
    CanonicalPageRead,
)

router = APIRouter(tags=["canonical-documents"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
Storage = Annotated[S3ObjectStorage, Depends(get_object_storage)]


@router.get(
    "/submissions/{submission_id}/canonical-document",
    response_model=CanonicalDocumentStatusRead,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": CanonicalErrorResponse,
            "description": "提交记录不存在",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": CanonicalErrorResponse,
            "description": "数据库暂时不可用",
        },
    },
)
async def get_canonical_document_status(
    submission_id: UUID,
    session: DbSession,
) -> CanonicalDocumentStatusRead:
    """Return status and safe metadata only; the full document stays in object storage."""
    return await service.get_status(session, submission_id)


@router.get(
    "/submissions/{submission_id}/canonical-document/pages/{page_number}",
    response_model=CanonicalPageRead,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": CanonicalErrorResponse,
            "description": "提交记录或页码不存在",
        },
        status.HTTP_409_CONFLICT: {
            "model": CanonicalErrorResponse,
            "description": "规范化结果尚未生成或生成失败",
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": CanonicalErrorResponse,
            "description": "对象存储暂时不可用或Canonical产物校验失败"
        },
    },
)
async def get_canonical_document_page(
    submission_id: UUID,
    page_number: Annotated[int, Path(ge=1)],
    session: DbSession,
    storage: Storage,
) -> CanonicalPageRead:
    """Return one verified page and never expose private object keys."""
    return await service.get_page(session, storage, submission_id, page_number)
