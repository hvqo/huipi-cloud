"""Short PostgreSQL transactions for visual proposal indexes."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.modules.answer_alignment.matcher import assignment_questions_digest
from huipi_cloud.modules.answer_alignment.models import AnswerAlignmentArtifact
from huipi_cloud.modules.answer_alignment.protocol import ALIGNER_VERSION
from huipi_cloud.modules.answer_alignment.repository import AlignmentContext, get_context
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact
from huipi_cloud.modules.submissions.models import Submission, SubmissionFile
from huipi_cloud.modules.visual_evidence.errors import (
    VisualEvidenceConflictError,
    VisualEvidenceNotFoundError,
    VisualEvidenceSourceChangedError,
)
from huipi_cloud.modules.visual_evidence.models import VisualEvidenceArtifact

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class VisualContext:
    alignment: AlignmentContext
    source_file: SubmissionFile
    canonical: CanonicalArtifact
    alignment_artifact: AnswerAlignmentArtifact


async def load_context(session: AsyncSession, submission_id: UUID) -> VisualContext:
    alignment = await get_context(session, submission_id)
    if alignment is None:
        raise VisualEvidenceNotFoundError
    if alignment.canonical_artifact is None or alignment.canonical_artifact.status != "available":
        raise VisualEvidenceSourceChangedError("canonical_not_available")
    canonical = alignment.canonical_artifact
    digest = assignment_questions_digest(alignment.questions)
    artifact = await session.scalar(
        select(AnswerAlignmentArtifact).where(
            AnswerAlignmentArtifact.canonical_artifact_id == canonical.id,
            AnswerAlignmentArtifact.aligner_version == ALIGNER_VERSION,
            AnswerAlignmentArtifact.assignment_questions_digest == digest,
        )
    )
    source_file = await session.scalar(
        select(SubmissionFile).where(SubmissionFile.submission_id == submission_id)
    )
    if artifact is None or source_file is None:
        raise VisualEvidenceSourceChangedError("current_source_not_ready")
    if (
        artifact.bucket != canonical.bucket
        or artifact.canonical_sha256 != canonical.canonical_sha256
    ):
        raise VisualEvidenceSourceChangedError("alignment_canonical_mismatch")
    return VisualContext(alignment, source_file, canonical, artifact)


async def get_request(
    session: AsyncSession,
    *,
    submission_id: UUID,
    question_id: UUID,
    request_id: UUID,
) -> VisualEvidenceArtifact | None:
    return await session.scalar(
        select(VisualEvidenceArtifact).where(
            VisualEvidenceArtifact.submission_id == submission_id,
            VisualEvidenceArtifact.question_id == question_id,
            VisualEvidenceArtifact.request_id == request_id,
        )
    )


async def is_object_indexed(
    session: AsyncSession,
    *,
    bucket: str,
    object_key: str,
) -> bool:
    return bool(
        await session.scalar(
            select(VisualEvidenceArtifact.id).where(
                VisualEvidenceArtifact.proposal_bucket == bucket,
                VisualEvidenceArtifact.proposal_object_key == object_key,
            )
        )
    )


async def register_success(
    session_factory: SessionFactory,
    *,
    values: dict[str, object],
    expected_question_stem: str,
    expected_source_file_id: UUID,
    expected_source_object_key: str,
    expected_source_bucket: str,
    expected_source_size: int,
) -> VisualEvidenceArtifact:
    """Revalidate current source versions and insert once in one PG transaction."""
    async with session_factory() as session, session.begin():
        submission_id = values["submission_id"]
        question_id = values["question_id"]
        request_id = values["request_id"]
        assert isinstance(submission_id, UUID)
        assert isinstance(question_id, UUID)
        assert isinstance(request_id, UUID)
        submission = await session.scalar(
            select(Submission).where(Submission.id == submission_id).with_for_update()
        )
        if submission is None:
            raise VisualEvidenceSourceChangedError("submission_removed_during_model_call")
        assignment = await session.scalar(
            select(Assignment)
            .where(Assignment.id == submission.assignment_id)
            .with_for_update()
        )
        if assignment is None or assignment.id != values["assignment_id"]:
            raise VisualEvidenceSourceChangedError("assignment_changed_during_model_call")
        context = await load_context(session, submission_id)
        question = next(
            (item for item in context.alignment.questions if item.id == question_id),
            None,
        )
        if question is None:
            raise VisualEvidenceSourceChangedError("question_removed_or_changed")
        if question.stem != expected_question_stem:
            raise VisualEvidenceSourceChangedError("question_text_changed")
        current_canonical = await session.scalar(
            select(CanonicalArtifact)
            .where(CanonicalArtifact.id == context.canonical.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        current_alignment = await session.scalar(
            select(AnswerAlignmentArtifact)
            .where(AnswerAlignmentArtifact.id == context.alignment_artifact.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        current_file = await session.scalar(
            select(SubmissionFile)
            .where(SubmissionFile.id == context.source_file.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if (
            current_file is None
            or current_canonical is None
            or current_alignment is None
            or current_file.id != expected_source_file_id
            or current_file.object_key != expected_source_object_key
            or current_file.bucket != expected_source_bucket
            or current_file.size_bytes != expected_source_size
            or current_file.sha256 != values["original_file_sha256"]
            or context.alignment.assignment_id != values["assignment_id"]
            or current_canonical.id != values["canonical_artifact_id"]
            or current_canonical.canonical_sha256 != values["canonical_sha256"]
            or current_alignment.id != values["alignment_artifact_id"]
            or current_alignment.alignment_sha256 != values["alignment_sha256"]
        ):
            raise VisualEvidenceSourceChangedError

        statement = (
            insert(VisualEvidenceArtifact)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_visual_evidence_submission_question_request")
        )
        await session.execute(statement)
        existing = await get_request(
            session,
            submission_id=submission_id,
            question_id=question_id,
            request_id=request_id,
        )
        if existing is None:
            raise RuntimeError("visual evidence registration did not produce an index")
        if existing.model_input_digest != values["model_input_digest"]:
            raise VisualEvidenceConflictError
        return existing
