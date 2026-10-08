"""Focused PostgreSQL queries for submissions."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.submissions.models import Submission


async def get_assignment_status(session: AsyncSession, assignment_id: UUID) -> str | None:
    return await session.scalar(
        select(Assignment.status).where(Assignment.id == assignment_id)
    )


async def get_submission(session: AsyncSession, submission_id: UUID) -> Submission | None:
    result = await session.scalars(
        select(Submission)
        .options(
            selectinload(Submission.file),
            selectinload(Submission.parsing_task),
        )
        .where(Submission.id == submission_id)
    )
    return result.unique().one_or_none()


async def list_assignment_submissions(
    session: AsyncSession,
    assignment_id: UUID,
    *,
    limit: int,
    offset: int,
) -> list[Submission]:
    result = await session.scalars(
        select(Submission)
        .options(
            selectinload(Submission.file),
            selectinload(Submission.parsing_task),
        )
        .where(Submission.assignment_id == assignment_id)
        .order_by(Submission.submitted_at.desc(), Submission.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.unique().all())
