"""Short PostgreSQL transactions for immutable GradeReview revisions."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import literal_column, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.modules.assignments.models import Question
from huipi_cloud.modules.grading import repository as grade_input_repository
from huipi_cloud.modules.grading.errors import GradeInputNotFoundError, GradeInputSourceChangedError
from huipi_cloud.modules.grading.grade_review_errors import (
    GradeReviewCommitOutcomeUnknownError,
    GradeReviewConflictError,
    GradeReviewIdempotencyConflictError,
    GradeReviewNotFoundError,
    GradeReviewRegistrationRolledBackError,
    GradeReviewSourceStaleError,
)
from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision
from huipi_cloud.modules.grading.grade_review_protocol import GradeReviewRevisionDocument
from huipi_cloud.modules.grading.models import GradeInputArtifact, GradeProposalArtifact
from huipi_cloud.modules.grading.protocol import GradeInputCore
from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.identity.service import require_assignment
from huipi_cloud.modules.submissions.models import Submission

SessionFactory = async_sessionmaker[AsyncSession]
logger = logging.getLogger(__name__)


async def get_proposal(session: AsyncSession, proposal_id: UUID) -> GradeProposalArtifact | None:
    return await session.scalar(
        select(GradeProposalArtifact).where(GradeProposalArtifact.id == proposal_id)
    )


async def get_review_by_request(
    session_factory: SessionFactory,
    *,
    proposal_id: UUID,
    request_id: UUID,
    principal: Principal | None = None,
) -> GradeReviewRevision | None:
    async with session_factory() as session, session.begin():
        if principal is not None:
            await authorize_proposal(session, proposal_id, principal, write=True, lock=True)
        return await session.scalar(
            select(GradeReviewRevision).where(
                GradeReviewRevision.proposal_id == proposal_id,
                GradeReviewRevision.request_id == request_id,
            )
        )


async def list_reviews(
    session_factory: SessionFactory,
    proposal_id: UUID,
) -> list[GradeReviewRevision]:
    async with session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(GradeReviewRevision)
                    .where(GradeReviewRevision.proposal_id == proposal_id)
                    .order_by(GradeReviewRevision.review_revision)
                )
            ).all()
        )


async def append_review(
    session_factory: SessionFactory,
    *,
    proposal_id: UUID,
    request_id: UUID,
    request_sha256: str,
    expected_revision: int,
    grade_input_document,
    review_draft: GradeReviewRevisionDocument,
    principal: Principal | None = None,
) -> tuple[GradeReviewRevision, bool]:
    """Lock one proposal, recheck all GradeInput sources, then append a revision."""

    commit_attempted = False
    try:
        async with session_factory() as session:
            transaction = await session.begin()
            try:
                if principal is not None:
                    await session.execute(text("SET LOCAL lock_timeout = '3000ms'"))
                    await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
                proposal = await session.scalar(
                    select(GradeProposalArtifact)
                    .where(GradeProposalArtifact.id == proposal_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if proposal is None:
                    raise GradeReviewNotFoundError("proposal_not_found")

                if principal is not None:
                    await authorize_proposal(session, proposal_id, principal, write=True, lock=True)
                replay = await session.scalar(
                    select(GradeReviewRevision).where(
                        GradeReviewRevision.proposal_id == proposal_id,
                        GradeReviewRevision.request_id == request_id,
                    )
                )
                if replay is not None:
                    if replay.request_sha256 != request_sha256:
                        raise GradeReviewIdempotencyConflictError("request_id_payload_conflict")
                    commit_attempted = True
                    await transaction.commit()
                    return replay, False

                if (
                    proposal.grade_input_artifact_id != grade_input_document.artifact_id
                    or proposal.grade_input_sha256 != grade_input_document.input_sha256
                    or proposal.submission_id != grade_input_document.submission_id
                    or proposal.question_id != grade_input_document.question.question_id
                    or proposal.object_sha256 != review_draft.proposal_sha256
                ):
                    raise GradeReviewSourceStaleError("proposal_index_binding_changed")

                indexed_input = await session.scalar(
                    select(GradeInputArtifact)
                    .where(GradeInputArtifact.id == grade_input_document.artifact_id)
                    .with_for_update(read=True)
                    .execution_options(populate_existing=True)
                )
                if (
                    indexed_input is None
                    or indexed_input.input_sha256 != grade_input_document.input_sha256
                    or indexed_input.submission_id != grade_input_document.submission_id
                    or indexed_input.question_id != grade_input_document.question.question_id
                ):
                    raise GradeReviewSourceStaleError("grade_input_index_changed")

                core = GradeInputCore.model_validate(
                    grade_input_document.model_dump(
                        exclude={"artifact_id", "input_sha256", "created_at"}
                    )
                )
                await grade_input_repository.lock_and_validate_sources(session, core)

                latest = await session.scalar(
                    select(GradeReviewRevision)
                    .where(GradeReviewRevision.proposal_id == proposal_id)
                    .order_by(GradeReviewRevision.review_revision.desc())
                    .limit(1)
                )
                latest_revision = 0 if latest is None else latest.review_revision
                if latest_revision != expected_revision:
                    raise GradeReviewConflictError("expected_revision_mismatch")

                revision = latest_revision + 1
                document = review_draft.model_copy(
                    update={
                        "review_revision": revision,
                        "supersedes_review_id": None if latest is None else latest.review_id,
                    }
                )
                record = GradeReviewRevision(
                    review_id=document.review_id,
                    proposal_id=document.proposal_id,
                    grade_input_artifact_id=document.grade_input_artifact_id,
                    proposal_sha256=document.proposal_sha256,
                    grade_input_sha256=document.grade_input_sha256,
                    submission_id=document.submission_id,
                    question_id=document.question_id,
                    review_schema_version=document.schema_version,
                    review_revision=document.review_revision,
                    supersedes_review_id=document.supersedes_review_id,
                    reviewer_ref=document.reviewer_ref,
                    reviewer_user_id=principal.user_id if principal else None,
                    request_id=document.request_id,
                    request_sha256=document.request_sha256,
                    review_status=document.review_status,
                    criterion_reviews=[
                        item.model_dump(mode="json") for item in document.criterion_reviews
                    ],
                    review_reason_codes=list(document.review_reason_codes),
                    review_comment=document.review_comment,
                    proposed_total_score=document.proposed_total_score,
                    reviewed_total_score=document.reviewed_total_score,
                    created_at=document.created_at,
                )
                session.add(record)
                await session.flush()
            except Exception as error:
                await _rollback_before_commit(transaction)
                if isinstance(error, GradeInputNotFoundError):
                    raise GradeReviewSourceStaleError("grade_input_source_missing") from error
                if isinstance(error, GradeInputSourceChangedError):
                    raise GradeReviewSourceStaleError(error.reason_code) from error
                if isinstance(error, IntegrityError):
                    raise GradeReviewConflictError("review_unique_constraint_conflict") from error
                raise

            commit_attempted = True
            await transaction.commit()
            return record, True
    except Exception as error:
        if commit_attempted:
            raise GradeReviewCommitOutcomeUnknownError from error
        if isinstance(
            error,
            (
                AccessError,
                GradeReviewNotFoundError,
                GradeReviewSourceStaleError,
                GradeReviewConflictError,
                GradeReviewIdempotencyConflictError,
            ),
        ):
            raise
        if isinstance(error, DBAPIError):
            raise GradeReviewRegistrationRolledBackError from error
        if isinstance(error, GradeReviewRegistrationRolledBackError):
            raise
        raise GradeReviewRegistrationRolledBackError from error


async def _rollback_before_commit(transaction) -> None:
    if not transaction.is_active:
        return
    try:
        await transaction.rollback()
    except Exception as error:
        logger.warning("GradeReview pre-commit rollback failed (%s)", type(error).__name__)


async def authorize_proposal(
    session: AsyncSession,
    proposal_id: UUID,
    principal: Principal,
    *,
    write: bool = False,
    lock: bool = False,
    submission_id: UUID | None = None,
    question_id: UUID | None = None,
) -> None:
    """Resolve all real resource bindings before object reads, history, or replay."""
    if lock:
        await session.execute(text("SET LOCAL lock_timeout = '3000ms'"))
        await session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    query = (
        select(GradeInputArtifact.assignment_id)
        .select_from(GradeProposalArtifact)
        .join(
            GradeInputArtifact,
            GradeInputArtifact.id == GradeProposalArtifact.grade_input_artifact_id,
        )
        .join(Submission, Submission.id == GradeProposalArtifact.submission_id)
        .join(Question, Question.id == GradeProposalArtifact.question_id)
        .where(
            GradeProposalArtifact.id == proposal_id,
            GradeInputArtifact.input_sha256 == GradeProposalArtifact.grade_input_sha256,
            GradeInputArtifact.submission_id == GradeProposalArtifact.submission_id,
            GradeInputArtifact.question_id == GradeProposalArtifact.question_id,
            GradeInputArtifact.assignment_id == Submission.assignment_id,
            GradeInputArtifact.assignment_id == Question.assignment_id,
        )
    )
    if submission_id is not None:
        query = query.where(GradeProposalArtifact.submission_id == submission_id)
    if question_id is not None:
        query = query.where(GradeProposalArtifact.question_id == question_id)
    assignment_id = await session.scalar(query)
    if assignment_id is None:
        raise AccessError("resource_not_found_or_forbidden", 404)
    await require_assignment(session, principal, assignment_id, write=write, lock=lock)


def summary_query():
    # Project only bounded score metadata in PostgreSQL: never fetch long rationales/quotes.
    scores = literal_column("""(SELECT coalesce(jsonb_agg(jsonb_build_object(
        'criterion_id', c->'criterion_id', 'proposed_points', c->'proposed_points',
        'reviewed_points', c->'reviewed_points', 'max_points', c->'max_points',
        'changed', c->'changed')), '[]'::jsonb)
        FROM (SELECT value AS c FROM jsonb_array_elements(criterion_reviews) LIMIT 100) items)
        """).label("criterion_scores")
    fields = (
        "review_id",
        "proposal_id",
        "grade_input_artifact_id",
        "proposal_sha256",
        "grade_input_sha256",
        "review_revision",
        "supersedes_review_id",
        "reviewer_ref",
        "reviewer_user_id",
        "request_id",
        "review_status",
        "proposed_total_score",
        "reviewed_total_score",
        "created_at",
        "review_reason_codes",
    )
    return select(
        *(getattr(GradeReviewRevision, name) for name in fields),
        scores,
        GradeReviewRevision.review_comment.is_not(None).label("review_comment_present"),
    )


async def review_page(session, proposal_id, *, limit=10, offset=0):
    return (
        (
            await session.execute(
                summary_query()
                .where(GradeReviewRevision.proposal_id == proposal_id)
                .order_by(GradeReviewRevision.review_revision.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        )
        .mappings()
        .all()
    )


async def review_summary(session, review_id):
    return (
        (await session.execute(summary_query().where(GradeReviewRevision.review_id == review_id)))
        .mappings()
        .one()
    )
