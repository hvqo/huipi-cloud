"""Focused database queries for the assignment domain."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from huipi_cloud.modules.assignments.models import (
    AnswerKey,
    Assignment,
    Question,
)


def assignment_detail_options():
    questions = selectinload(Assignment.questions)
    return (
        questions.selectinload(Question.answer_key),
        questions.selectinload(Question.rubric_criteria),
    )


async def list_assignments(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
) -> list[Assignment]:
    result = await session.scalars(
        select(Assignment)
        .order_by(Assignment.created_at.desc(), Assignment.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.all())


async def get_assignment(
    session: AsyncSession,
    assignment_id: UUID,
) -> Assignment | None:
    result = await session.scalars(
        select(Assignment)
        .options(*assignment_detail_options())
        .where(Assignment.id == assignment_id)
    )
    return result.unique().one_or_none()


async def get_assignment_for_update(
    session: AsyncSession,
    assignment_id: UUID,
) -> Assignment | None:
    result = await session.scalars(
        select(Assignment)
        .where(Assignment.id == assignment_id)
        .with_for_update()
    )
    return result.one_or_none()


async def get_question(
    session: AsyncSession,
    question_id: UUID,
) -> Question | None:
    result = await session.scalars(
        select(Question)
        .options(
            selectinload(Question.answer_key),
            selectinload(Question.rubric_criteria),
        )
        .where(Question.id == question_id)
    )
    return result.unique().one_or_none()


async def get_question_assignment_id(
    session: AsyncSession,
    question_id: UUID,
) -> UUID | None:
    return await session.scalar(
        select(Question.assignment_id).where(Question.id == question_id)
    )


async def question_number_exists(
    session: AsyncSession,
    assignment_id: UUID,
    question_number: int,
) -> bool:
    return bool(
        await session.scalar(
            select(Question.id)
            .where(
                Question.assignment_id == assignment_id,
                Question.question_number == question_number,
            )
            .limit(1)
        )
    )


async def get_answer_key(
    session: AsyncSession,
    question_id: UUID,
) -> AnswerKey | None:
    result = await session.scalars(
        select(AnswerKey).where(AnswerKey.question_id == question_id)
    )
    return result.one_or_none()
