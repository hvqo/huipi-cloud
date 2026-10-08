"""Safe parsing task status endpoint."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.modules.assignments.schemas import ErrorResponse
from huipi_cloud.modules.parsing import service
from huipi_cloud.modules.parsing.schemas import ParsingTaskStatusRead

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
