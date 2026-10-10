"""Transactional PostgreSQL reads and append-only answer-review revisions."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.modules.answer_alignment import repository as alignment_repository
from huipi_cloud.modules.answer_alignment.matcher import assignment_questions_digest
from huipi_cloud.modules.answer_alignment.models import AnswerAlignmentArtifact
from huipi_cloud.modules.answer_alignment.protocol import ALIGNER_VERSION
from huipi_cloud.modules.answer_review.errors import (
    AnswerReviewIdempotencyConflictError,
    AnswerReviewQuestionNotFoundError,
    AnswerReviewSourceChangedError,
    AnswerReviewSourceNotReadyError,
    AnswerReviewSubmissionNotFoundError,
)
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact
from huipi_cloud.modules.submissions.models import Submission

SessionFactory = async_sessionmaker[AsyncSession]


async def append_decision(
    session_factory: SessionFactory,
    *,
    submission_id: UUID,
    assignment_id: UUID,
    question_id: UUID,
    alignment_artifact_id: UUID,
    alignment_sha256: str,
    alignment_status: str,
    aligner_version: str,
    assignment_questions_digest_value: str,
    canonical_document_id: UUID,
    canonical_sha256: str,
    review_schema_version: str,
    decision: str,
    response_regions: list[dict],
    excluded_prompt_regions: list[dict],
    uncertain_regions: list[dict],
    reason_codes: list[str],
    reviewer_ref: str,
    request_id: UUID,
    request_sha256: str,
) -> tuple[AnswerReviewDecision, bool]:
    """Append a revision while serializing updates for one Submission.

    The Submission row lock assigns a single monotonic revision chain. The
    Assignment row lock matches P1-A's question-mutation boundary. Before
    inserting, this transaction verifies that the canonical/alignment/question
    version supplied by the caller is still current.
    """

    async with session_factory() as session, session.begin():
        submission = await session.scalar(
            select(Submission).where(Submission.id == submission_id).with_for_update()
        )
        if submission is None:
            raise AnswerReviewSubmissionNotFoundError
        if submission.assignment_id != assignment_id:
            raise AnswerReviewSourceChangedError

        replay = await session.scalar(
            select(AnswerReviewDecision).where(
                AnswerReviewDecision.submission_id == submission_id,
                AnswerReviewDecision.question_id == question_id,
                AnswerReviewDecision.request_id == request_id,
            )
        )
        if replay is not None:
            if replay.request_sha256 != request_sha256:
                raise AnswerReviewIdempotencyConflictError
            return replay, False

        assignment = await session.scalar(
            select(Assignment).where(Assignment.id == assignment_id).with_for_update()
        )
        if assignment is None:
            raise AnswerReviewSourceChangedError

        context = await alignment_repository.get_context(session, submission_id)
        if context is None or context.canonical_artifact is None:
            raise AnswerReviewSourceNotReadyError
        canonical: CanonicalArtifact | None = await session.scalar(
            select(CanonicalArtifact)
            .where(CanonicalArtifact.id == context.canonical_artifact.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if canonical is None:
            raise AnswerReviewSourceChangedError
        current_digest = assignment_questions_digest(context.questions)
        current_alignment = await session.scalar(
            select(AnswerAlignmentArtifact)
            .where(
                AnswerAlignmentArtifact.canonical_artifact_id == canonical.id,
                AnswerAlignmentArtifact.aligner_version == ALIGNER_VERSION,
                AnswerAlignmentArtifact.assignment_questions_digest == current_digest,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if (
            context.parsing_status != "succeeded"
            or canonical.status != "available"
            or canonical.canonical_sha256 != canonical_sha256
            or canonical.id != canonical_document_id
            or context.assignment_id != assignment_id
            or current_digest != assignment_questions_digest_value
            or current_alignment is None
            or current_alignment.id != alignment_artifact_id
            or current_alignment.alignment_sha256 != alignment_sha256
            or current_alignment.aligner_version != aligner_version
        ):
            raise AnswerReviewSourceChangedError

        question = next((item for item in context.questions if item.id == question_id), None)
        if question is None or question.assignment_id != assignment_id:
            raise AnswerReviewQuestionNotFoundError

        latest = await session.scalar(
            select(AnswerReviewDecision)
            .where(
                AnswerReviewDecision.submission_id == submission_id,
                AnswerReviewDecision.question_id == question_id,
            )
            .order_by(AnswerReviewDecision.revision.desc())
            .limit(1)
        )
        record = AnswerReviewDecision(
            submission_id=submission_id,
            assignment_id=assignment_id,
            question_id=question_id,
            question_number=question.question_number,
            alignment_status=alignment_status,
            alignment_artifact_id=alignment_artifact_id,
            alignment_sha256=alignment_sha256,
            aligner_version=aligner_version,
            assignment_questions_digest=assignment_questions_digest_value,
            canonical_document_id=canonical_document_id,
            canonical_sha256=canonical_sha256,
            review_schema_version=review_schema_version,
            decision=decision,
            response_regions=response_regions,
            excluded_prompt_regions=excluded_prompt_regions,
            uncertain_regions=uncertain_regions,
            reason_codes=reason_codes,
            reviewer_ref=reviewer_ref,
            request_id=request_id,
            request_sha256=request_sha256,
            revision=1 if latest is None else latest.revision + 1,
            supersedes_decision_id=None if latest is None else latest.id,
        )
        session.add(record)
        await session.flush()
        return record, True


async def get_submission(
    session: AsyncSession,
    submission_id: UUID,
) -> Submission | None:
    return await session.scalar(select(Submission).where(Submission.id == submission_id))


async def list_decisions(
    session: AsyncSession,
    submission_id: UUID,
    *,
    question_id: UUID | None = None,
) -> list[AnswerReviewDecision]:
    statement = select(AnswerReviewDecision).where(
        AnswerReviewDecision.submission_id == submission_id
    )
    if question_id is not None:
        statement = statement.where(AnswerReviewDecision.question_id == question_id)
    return list(
        (
            await session.scalars(
                statement.order_by(
                    AnswerReviewDecision.question_number,
                    AnswerReviewDecision.revision,
                )
            )
        ).all()
    )
