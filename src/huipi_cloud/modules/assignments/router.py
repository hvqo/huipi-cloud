"""REST endpoints for teacher-side assignment management."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.modules.assignments import service
from huipi_cloud.modules.assignments.schemas import (
    AnswerKeyRead,
    AnswerKeyReplace,
    AssignmentCreate,
    AssignmentDetail,
    AssignmentSummary,
    ErrorResponse,
    QuestionCreate,
    QuestionRead,
    RubricRead,
    RubricReplace,
)

assignment_router = APIRouter(prefix="/assignments", tags=["assignments"])
question_router = APIRouter(tags=["questions"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
DATABASE_ERROR_RESPONSES = {
    500: {"model": ErrorResponse, "description": "数据库操作失败"},
    503: {"model": ErrorResponse, "description": "数据库暂时不可用"},
}
DOMAIN_ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "资源不存在"},
    409: {"model": ErrorResponse, "description": "请求与当前资源状态冲突"},
}


@assignment_router.post(
    "",
    response_model=AssignmentDetail,
    status_code=status.HTTP_201_CREATED,
    responses=DATABASE_ERROR_RESPONSES,
)
async def create_assignment(payload: AssignmentCreate, session: DbSession) -> AssignmentDetail:
    return await service.create_assignment(session, payload)


@assignment_router.get(
    "",
    response_model=list[AssignmentSummary],
    responses=DATABASE_ERROR_RESPONSES,
)
async def list_assignments(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AssignmentSummary]:
    return await service.list_assignments(session, limit=limit, offset=offset)


@assignment_router.get(
    "/{assignment_id}",
    response_model=AssignmentDetail,
    responses={**DOMAIN_ERROR_RESPONSES, **DATABASE_ERROR_RESPONSES},
)
async def get_assignment(assignment_id: UUID, session: DbSession) -> AssignmentDetail:
    return await service.get_assignment(session, assignment_id)


@assignment_router.post(
    "/{assignment_id}/questions",
    response_model=QuestionRead,
    status_code=status.HTTP_201_CREATED,
    responses={**DOMAIN_ERROR_RESPONSES, **DATABASE_ERROR_RESPONSES},
)
async def add_question(
    assignment_id: UUID,
    payload: QuestionCreate,
    session: DbSession,
) -> QuestionRead:
    return await service.add_question(session, assignment_id, payload)


@question_router.put(
    "/questions/{question_id}/answer-key",
    response_model=AnswerKeyRead,
    responses={**DOMAIN_ERROR_RESPONSES, **DATABASE_ERROR_RESPONSES},
)
async def replace_answer_key(
    question_id: UUID,
    payload: AnswerKeyReplace,
    session: DbSession,
) -> AnswerKeyRead:
    return await service.replace_answer_key(session, question_id, payload)


@question_router.put(
    "/questions/{question_id}/rubric",
    response_model=RubricRead,
    responses={**DOMAIN_ERROR_RESPONSES, **DATABASE_ERROR_RESPONSES},
)
async def replace_rubric(
    question_id: UUID,
    payload: RubricReplace,
    session: DbSession,
) -> RubricRead:
    criteria = await service.replace_rubric(session, question_id, payload)
    return RubricRead(criteria=criteria)


@assignment_router.post(
    "/{assignment_id}/publish",
    response_model=AssignmentDetail,
    responses={**DOMAIN_ERROR_RESPONSES, **DATABASE_ERROR_RESPONSES},
)
async def publish_assignment(
    assignment_id: UUID,
    session: DbSession,
) -> AssignmentDetail:
    return await service.publish_assignment(session, assignment_id)
