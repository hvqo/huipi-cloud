"""Assignment use cases and transaction boundaries."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.infrastructure.database.base import utc_now
from huipi_cloud.modules.assignments import repository
from huipi_cloud.modules.assignments.enums import AnswerSource, AssignmentStatus
from huipi_cloud.modules.assignments.errors import (
    AssignmentConflictError,
    AssignmentNotFoundError,
    AssignmentValidationError,
)
from huipi_cloud.modules.assignments.models import (
    AnswerKey,
    Assignment,
    Question,
    RubricCriterion,
)
from huipi_cloud.modules.assignments.schemas import (
    AnswerKeyReplace,
    AssignmentCreate,
    QuestionCreate,
    RubricReplace,
)


def _require_draft(assignment: Assignment) -> None:
    if assignment.status != AssignmentStatus.DRAFT:
        raise AssignmentConflictError("只有草稿作业可以修改或发布")


async def create_assignment(
    session: AsyncSession,
    payload: AssignmentCreate,
) -> Assignment:
    async with session.begin():
        assignment = Assignment(**payload.model_dump())
        session.add(assignment)
        await session.flush()
        assignment_id = assignment.id

    result = await repository.get_assignment(session, assignment_id)
    assert result is not None
    return result


async def list_assignments(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
) -> list[Assignment]:
    return await repository.list_assignments(session, limit=limit, offset=offset)


async def get_assignment(session: AsyncSession, assignment_id: UUID) -> Assignment:
    assignment = await repository.get_assignment(session, assignment_id)
    if assignment is None:
        raise AssignmentNotFoundError("作业不存在")
    return assignment


async def add_question(
    session: AsyncSession,
    assignment_id: UUID,
    payload: QuestionCreate,
) -> Question:
    async with session.begin():
        assignment = await repository.get_assignment_for_update(session, assignment_id)
        if assignment is None:
            raise AssignmentNotFoundError("作业不存在")
        _require_draft(assignment)
        if await repository.question_number_exists(
            session,
            assignment_id,
            payload.question_number,
        ):
            raise AssignmentConflictError("该作业已存在相同题号")

        question = Question(
            assignment_id=assignment_id,
            question_number=payload.question_number,
            question_type=payload.question_type.value,
            stem=payload.stem,
            max_score=payload.max_score,
        )
        assignment.updated_at = utc_now()
        session.add(question)
        await session.flush()
        question_id = question.id

    result = await repository.get_question(session, question_id)
    assert result is not None
    return result


async def replace_answer_key(
    session: AsyncSession,
    question_id: UUID,
    payload: AnswerKeyReplace,
) -> AnswerKey:
    async with session.begin():
        assignment_id = await repository.get_question_assignment_id(session, question_id)
        if assignment_id is None:
            raise AssignmentNotFoundError("题目不存在")
        assignment = await repository.get_assignment_for_update(session, assignment_id)
        if assignment is None:
            raise AssignmentNotFoundError("作业不存在")
        _require_draft(assignment)

        question = await repository.get_question(session, question_id)
        if question is None:
            raise AssignmentNotFoundError("题目不存在")

        answer_key = await repository.get_answer_key(session, question_id)
        if answer_key is None:
            answer_key = AnswerKey(
                question_id=question_id,
                answer_content=payload.answer_content,
                source=AnswerSource.TEACHER.value,
            )
            session.add(answer_key)
        else:
            answer_key.answer_content = payload.answer_content
            answer_key.source = AnswerSource.TEACHER.value

        assignment.updated_at = utc_now()
        await session.flush()
        answer_key_id = answer_key.id

    result = await session.get(AnswerKey, answer_key_id)
    assert result is not None
    return result


async def replace_rubric(
    session: AsyncSession,
    question_id: UUID,
    payload: RubricReplace,
) -> list[RubricCriterion]:
    async with session.begin():
        assignment_id = await repository.get_question_assignment_id(session, question_id)
        if assignment_id is None:
            raise AssignmentNotFoundError("题目不存在")
        assignment = await repository.get_assignment_for_update(session, assignment_id)
        if assignment is None:
            raise AssignmentNotFoundError("作业不存在")
        _require_draft(assignment)

        question = await repository.get_question(session, question_id)
        if question is None:
            raise AssignmentNotFoundError("题目不存在")

        total_points = sum(
            (criterion.points for criterion in payload.criteria),
            start=Decimal("0.00"),
        )
        if total_points > question.max_score:
            raise AssignmentValidationError("评分细则总分不能超过题目满分")

        await session.execute(
            delete(RubricCriterion).where(RubricCriterion.question_id == question_id)
        )
        criteria = [
            RubricCriterion(
                question_id=question_id,
                description=item.description,
                points=item.points,
                sort_order=item.sort_order,
            )
            for item in payload.criteria
        ]
        session.add_all(criteria)
        assignment.updated_at = utc_now()
        await session.flush()
        criterion_ids = [criterion.id for criterion in criteria]

    return [
        criterion
        for criterion_id in criterion_ids
        if (criterion := await session.get(RubricCriterion, criterion_id)) is not None
    ]


async def publish_assignment(
    session: AsyncSession,
    assignment_id: UUID,
) -> Assignment:
    async with session.begin():
        assignment = await repository.get_assignment_for_update(session, assignment_id)
        if assignment is None:
            raise AssignmentNotFoundError("作业不存在")
        _require_draft(assignment)

        detail = await repository.get_assignment(session, assignment_id)
        assert detail is not None
        if not detail.questions:
            raise AssignmentConflictError("作业至少需要一道题目才能发布")

        for question in detail.questions:
            if question.answer_key is None:
                raise AssignmentConflictError(f"第 {question.question_number} 题尚未设置标准答案")
            if not question.rubric_criteria:
                raise AssignmentConflictError(f"第 {question.question_number} 题尚未设置评分细则")
            rubric_total = sum(
                (criterion.points for criterion in question.rubric_criteria),
                start=Decimal("0.00"),
            )
            if rubric_total != question.max_score:
                raise AssignmentConflictError(
                    f"第 {question.question_number} 题评分细则总分必须等于题目满分"
                )

        assignment.status = AssignmentStatus.PUBLISHED.value
        assignment.updated_at = utc_now()
        await session.flush()

    result = await repository.get_assignment(session, assignment_id)
    assert result is not None
    return result
