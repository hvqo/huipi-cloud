"""Bounded teacher projection. No AnswerKey, private keys or full submission contents."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from huipi_cloud.modules.assignments.models import Assignment, Question, RubricCriterion
from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.assignments.schemas import AssignmentSummary, RubricCriterionRead
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.identity.models import AssignmentMembership
from huipi_cloud.modules.identity.protocol import AccessError

router = APIRouter(prefix="/assignments", tags=["teacher-workbench"])


class WorkbenchQuestion(BaseModel):
    id: UUID
    question_number: int
    question_type: str
    stem: str
    max_score: str
    rubric_criteria: list[RubricCriterionRead] = Field(max_length=5)
    rubric_count: int
    rubric_truncated: bool


class AssignmentWorkbench(BaseModel):
    assignment: AssignmentSummary
    can_write: bool
    questions: list[WorkbenchQuestion] = Field(max_length=25)
    offset: int
    limit: int
    has_next: bool
    next_offset: int | None


@router.get("/{assignment_id}/workbench", response_model=AssignmentWorkbench)
async def get_assignment_workbench(
    assignment_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=25)] = 20,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    question_id: UUID | None = None,
):
    response.headers["Cache-Control"] = "private, no-store"
    assignment = await session.get(Assignment, assignment_id)
    if assignment is None:
        raise AccessError("resource_not_found_or_forbidden", 404)
    query = select(Question).where(Question.assignment_id == assignment_id)
    if question_id is not None:
        query = query.where(Question.id == question_id)
    questions = list(
        (
            await session.scalars(
                query.order_by(Question.question_number).offset(offset).limit(limit + 1)
            )
        ).all()
    )
    if question_id is not None and not questions:
        raise AccessError("resource_not_found_or_forbidden", 404)
    ranked = (
        select(
            RubricCriterion,
            func.count().over(partition_by=RubricCriterion.question_id).label("total"),
            func.row_number()
            .over(partition_by=RubricCriterion.question_id, order_by=RubricCriterion.sort_order)
            .label("position"),
        )
        .where(RubricCriterion.question_id.in_([q.id for q in questions[:limit]]))
        .subquery()
    )
    rubrics = (
        (
            await session.execute(
                select(ranked)
                .where(ranked.c.position <= 5)
                .order_by(ranked.c.question_id, ranked.c.position)
            )
        )
        .mappings()
        .all()
    )
    grant = await session.scalar(
        select(AssignmentMembership.can_write).where(
            AssignmentMembership.assignment_id == assignment_id,
            AssignmentMembership.user_id == principal.user_id,
            AssignmentMembership.revoked_at.is_(None),
        )
    )
    return AssignmentWorkbench(
        assignment=AssignmentSummary.model_validate(assignment),
        can_write=principal.is_admin or bool(grant),
        questions=[
            WorkbenchQuestion(
                id=q.id,
                question_number=q.question_number,
                question_type=q.question_type,
                stem=q.stem,
                max_score=str(q.max_score),
                rubric_criteria=[
                    RubricCriterionRead.model_validate(dict(r))
                    for r in rubrics
                    if r["question_id"] == q.id
                ],
                rubric_count=next((r["total"] for r in rubrics if r["question_id"] == q.id), 0),
                rubric_truncated=any(r["total"] > 5 for r in rubrics if r["question_id"] == q.id),
            )
            for q in questions[:limit]
        ],
        limit=limit,
        offset=offset,
        has_next=len(questions) > limit,
        next_offset=offset + limit if len(questions) > limit else None,
    )
