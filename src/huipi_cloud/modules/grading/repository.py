"""Short PostgreSQL transactions for immutable GradeInput indexes."""

from __future__ import annotations

import hashlib
import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.modules.answer_alignment.matcher import assignment_questions_digest
from huipi_cloud.modules.answer_alignment.models import AnswerAlignmentArtifact
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.answer_review.protocol import ReviewEvidenceRegion
from huipi_cloud.modules.assignments.enums import AssignmentStatus
from huipi_cloud.modules.assignments.models import (
    AnswerKey,
    Assignment,
    Question,
    RubricCriterion,
)
from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact
from huipi_cloud.modules.grading.errors import (
    GradeInputCommitOutcomeUnknownError,
    GradeInputIdempotencyConflictError,
    GradeInputNotFoundError,
    GradeInputRegistrationRolledBackError,
    GradeInputSourceChangedError,
)
from huipi_cloud.modules.grading.models import GradeInputArtifact
from huipi_cloud.modules.grading.protocol import (
    GradeInputCore,
    RubricCriterionSnapshot,
    json_sha256,
    rubric_sha256,
)
from huipi_cloud.modules.submissions.models import Submission, SubmissionFile

SessionFactory = async_sessionmaker[AsyncSession]
logger = logging.getLogger(__name__)


async def find_current_request(
    session_factory: SessionFactory,
    core: GradeInputCore,
    *,
    input_sha256: str,
) -> GradeInputArtifact | None:
    """Return a replay only after locking and rechecking every bound source."""

    async with session_factory() as session:
        async with session.begin():
            await lock_and_validate_sources(session, core)
            record = await session.scalar(
                select(GradeInputArtifact).where(
                    GradeInputArtifact.submission_id == core.submission_id,
                    GradeInputArtifact.question_id == core.question.question_id,
                    GradeInputArtifact.request_id == core.request_id,
                )
            )
            if record is not None and record.input_sha256 != input_sha256:
                raise GradeInputIdempotencyConflictError
            return record


async def register_success(
    session_factory: SessionFactory,
    *,
    core: GradeInputCore,
    input_sha256: str,
    artifact_id: UUID,
    bucket: str,
    object_key: str,
    object_sha256: str,
    object_size_bytes: int,
) -> tuple[GradeInputArtifact, bool]:
    """Revalidate source rows and register one immutable package.

    Failures before COMMIT are explicitly rolled back and classified. Once COMMIT
    is attempted, an exception is conservatively treated as an unknown outcome.
    """

    commit_attempted = False
    try:
        async with session_factory() as session:
            transaction = await session.begin()
            try:
                await lock_and_validate_sources(session, core)
                existing = await session.scalar(
                    select(GradeInputArtifact).where(
                        GradeInputArtifact.submission_id == core.submission_id,
                        GradeInputArtifact.question_id == core.question.question_id,
                        GradeInputArtifact.request_id == core.request_id,
                    )
                )
                if existing is not None:
                    if existing.input_sha256 != input_sha256:
                        raise GradeInputIdempotencyConflictError
                    # Commit this read-only verification transaction so the
                    # detached ORM row retains its loaded scalar attributes.
                    await transaction.commit()
                    return existing, False

                statement = (
                    insert(GradeInputArtifact)
                    .values(
                        id=artifact_id,
                        request_id=core.request_id,
                        submission_id=core.submission_id,
                        assignment_id=core.assignment_id,
                        question_id=core.question.question_id,
                        question_number=core.question.question_number,
                        canonical_artifact_id=core.canonical_artifact_id,
                        canonical_sha256=core.canonical_sha256,
                        alignment_artifact_id=core.alignment_artifact_id,
                        alignment_sha256=core.alignment_sha256,
                        assignment_questions_digest=core.assignment_questions_digest,
                        review_decision_id=core.review_decision_id,
                        review_revision=core.review_revision,
                        review_request_sha256=core.review_request_sha256,
                        source_file_sha256=core.source_file_sha256,
                        answer_key_id=core.answer_key.answer_key_id,
                        answer_key_sha256=core.answer_key.sha256,
                        rubric_sha256=core.rubric_sha256,
                        input_sha256=input_sha256,
                        schema_version=core.schema_version,
                        bucket=bucket,
                        object_key=object_key,
                        object_sha256=object_sha256,
                        object_size_bytes=object_size_bytes,
                    )
                    .on_conflict_do_nothing(
                        constraint="uq_grade_inputs_submission_question_request"
                    )
                )
                insert_result = await session.execute(statement)
                record = await session.scalar(
                    select(GradeInputArtifact).where(
                        GradeInputArtifact.submission_id == core.submission_id,
                        GradeInputArtifact.question_id == core.question.question_id,
                        GradeInputArtifact.request_id == core.request_id,
                    )
                )
                if record is None:
                    raise RuntimeError("GradeInput registration did not produce an index")
                if record.input_sha256 != input_sha256:
                    raise GradeInputIdempotencyConflictError
            except Exception:
                await _rollback_before_commit(transaction)
                raise

            commit_attempted = True
            await transaction.commit()
            return record, insert_result.rowcount == 1
    except Exception as error:
        if commit_attempted:
            raise GradeInputCommitOutcomeUnknownError from error
        if isinstance(
            error,
            (
                GradeInputNotFoundError,
                GradeInputSourceChangedError,
                GradeInputIdempotencyConflictError,
            ),
        ):
            raise
        raise GradeInputRegistrationRolledBackError from error


async def get_artifact(
    session: AsyncSession,
    artifact_id: UUID,
) -> GradeInputArtifact | None:
    return await session.scalar(
        select(GradeInputArtifact).where(GradeInputArtifact.id == artifact_id)
    )


async def lock_and_validate_sources(session: AsyncSession, core: GradeInputCore) -> None:
    """Use the same Submission/Assignment lock order as review and assignment writes."""

    submission = await session.scalar(
        select(Submission)
        .where(Submission.id == core.submission_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if submission is None:
        raise GradeInputNotFoundError
    if submission.assignment_id != core.assignment_id:
        raise GradeInputSourceChangedError("submission_assignment_changed")

    assignment = await session.scalar(
        select(Assignment)
        .where(Assignment.id == core.assignment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if assignment is None:
        raise GradeInputSourceChangedError("assignment_missing")
    if assignment.status != AssignmentStatus.PUBLISHED.value:
        raise GradeInputSourceChangedError("assignment_not_published")

    question = await session.scalar(
        select(Question)
        .where(Question.id == core.question.question_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if question is None or question.assignment_id != assignment.id:
        raise GradeInputNotFoundError
    if (
        question.question_number != core.question.question_number
        or question.question_type != core.question.question_type
        or question.stem != core.question.stem
        or question.max_score != core.question.max_score
    ):
        raise GradeInputSourceChangedError("question_snapshot_changed")

    questions = list(
        (
            await session.scalars(
                select(Question)
                .where(Question.assignment_id == assignment.id)
                .order_by(Question.question_number, Question.id)
            )
        ).all()
    )
    if assignment_questions_digest(questions) != core.assignment_questions_digest:
        raise GradeInputSourceChangedError("assignment_questions_changed")

    canonical = await session.scalar(
        select(CanonicalArtifact)
        .where(CanonicalArtifact.id == core.canonical_artifact_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        canonical is None
        or canonical.submission_id != submission.id
        or canonical.status != "available"
        or canonical.canonical_sha256 != core.canonical_sha256
        or canonical.schema_version != core.canonical_schema_version
        or canonical.source_sha256 != core.canonical_source_sha256
    ):
        raise GradeInputSourceChangedError("canonical_source_changed")

    alignment = await session.scalar(
        select(AnswerAlignmentArtifact)
        .where(AnswerAlignmentArtifact.id == core.alignment_artifact_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        alignment is None
        or alignment.submission_id != submission.id
        or alignment.assignment_id != assignment.id
        or alignment.canonical_artifact_id != canonical.id
        or alignment.canonical_sha256 != canonical.canonical_sha256
        or alignment.alignment_sha256 != core.alignment_sha256
        or alignment.aligner_version != core.aligner_version
        or alignment.assignment_questions_digest != core.assignment_questions_digest
    ):
        raise GradeInputSourceChangedError("alignment_source_changed")

    source_file = await session.scalar(
        select(SubmissionFile)
        .where(SubmissionFile.submission_id == submission.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if source_file is None or source_file.sha256 != core.source_file_sha256:
        raise GradeInputSourceChangedError("submission_file_changed")

    answer_key = await session.scalar(
        select(AnswerKey)
        .where(AnswerKey.question_id == question.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        answer_key is None
        or answer_key.id != core.answer_key.answer_key_id
        or answer_key.question_id != core.answer_key.question_id
        or answer_key.source != core.answer_key.source
        or answer_key.answer_content != core.answer_key.answer_content
        or hashlib.sha256(answer_key.answer_content.encode("utf-8")).hexdigest()
        != core.answer_key.sha256
    ):
        raise GradeInputSourceChangedError("answer_key_changed")

    criteria = list(
        (
            await session.scalars(
                select(RubricCriterion)
                .where(RubricCriterion.question_id == question.id)
                .order_by(RubricCriterion.sort_order, RubricCriterion.id)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    current_rubric = [
        RubricCriterionSnapshot(
            criterion_id=item.id,
            question_id=item.question_id,
            description=item.description,
            points=item.points,
            sort_order=item.sort_order,
        )
        for item in criteria
    ]
    if (
        current_rubric != core.rubric_snapshot
        or rubric_sha256(current_rubric) != core.rubric_sha256
    ):
        raise GradeInputSourceChangedError("rubric_snapshot_changed")

    latest_review = await session.scalar(
        select(AnswerReviewDecision)
        .where(
            AnswerReviewDecision.submission_id == submission.id,
            AnswerReviewDecision.question_id == question.id,
        )
        .order_by(AnswerReviewDecision.revision.desc())
        .limit(1)
    )
    if (
        latest_review is None
        or latest_review.id != core.review_decision_id
        or latest_review.revision != core.review_revision
        or latest_review.request_sha256 != core.review_request_sha256
        or latest_review.decision != "response_present"
        or latest_review.assignment_id != assignment.id
        or latest_review.question_id != question.id
        or latest_review.question_number != question.question_number
        or latest_review.aligner_version != core.aligner_version
        or latest_review.review_schema_version != core.review_schema_version
        or latest_review.canonical_document_id != canonical.id
        or latest_review.canonical_sha256 != canonical.canonical_sha256
        or latest_review.alignment_artifact_id != alignment.id
        or latest_review.alignment_sha256 != alignment.alignment_sha256
        or latest_review.assignment_questions_digest != core.assignment_questions_digest
    ):
        raise GradeInputSourceChangedError("human_review_changed")
    if _review_region_digest(latest_review.response_regions) != core.review_evidence_sha256:
        raise GradeInputSourceChangedError("human_response_regions_changed")
    if _review_region_digest(latest_review.excluded_prompt_regions) != (
        core.excluded_prompt_regions_sha256
    ):
        raise GradeInputSourceChangedError("human_prompt_regions_changed")


def _review_region_digest(value: list[dict]) -> str:
    normalized = [
        ReviewEvidenceRegion.model_validate(item).model_dump(mode="json") for item in value
    ]
    return json_sha256(normalized)


async def _rollback_before_commit(transaction) -> None:
    if not transaction.is_active:
        return
    try:
        await transaction.rollback()
    except Exception as error:
        logger.warning("GradeInput pre-commit rollback failed (%s)", type(error).__name__)
