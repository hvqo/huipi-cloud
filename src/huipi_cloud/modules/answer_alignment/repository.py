"""Focused PostgreSQL reads and idempotent Answer Alignment index registration."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.modules.answer_alignment.models import AnswerAlignmentArtifact
from huipi_cloud.modules.assignments.models import Question
from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact
from huipi_cloud.modules.canonical_documents.protocol import NORMALIZER_VERSION
from huipi_cloud.modules.parsing.models import ParsedArtifact
from huipi_cloud.modules.submissions.models import ParsingTask, Submission

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class AlignmentContext:
    submission_id: UUID
    assignment_id: UUID
    parsing_status: str | None
    canonical_artifact: CanonicalArtifact | None
    questions: list[Question]


async def get_context(
    session: AsyncSession,
    submission_id: UUID,
) -> AlignmentContext | None:
    result = await session.execute(
        select(
            Submission.id,
            Submission.assignment_id,
            ParsingTask.status,
            CanonicalArtifact,
        )
        .join(ParsingTask, ParsingTask.submission_id == Submission.id, isouter=True)
        .join(
            ParsedArtifact,
            (ParsedArtifact.submission_id == Submission.id)
            & (ParsedArtifact.parsing_task_id == ParsingTask.id),
            isouter=True,
        )
        .join(
            CanonicalArtifact,
            (CanonicalArtifact.parsed_artifact_id == ParsedArtifact.id)
            & (CanonicalArtifact.normalizer_version == NORMALIZER_VERSION),
            isouter=True,
        )
        .where(Submission.id == submission_id)
    )
    row = result.one_or_none()
    if row is None:
        return None
    db_submission_id, assignment_id, parsing_status, canonical = row
    questions = list(
        (
            await session.scalars(
                select(Question)
                .where(Question.assignment_id == assignment_id)
                .order_by(Question.question_number, Question.id)
            )
        ).all()
    )
    return AlignmentContext(
        submission_id=db_submission_id,
        assignment_id=assignment_id,
        parsing_status=parsing_status,
        canonical_artifact=canonical,
        questions=questions,
    )


async def get_artifact(
    session: AsyncSession,
    *,
    canonical_artifact_id: UUID,
    aligner_version: str,
    questions_digest: str,
) -> AnswerAlignmentArtifact | None:
    return await session.scalar(
        select(AnswerAlignmentArtifact).where(
            AnswerAlignmentArtifact.canonical_artifact_id == canonical_artifact_id,
            AnswerAlignmentArtifact.aligner_version == aligner_version,
            AnswerAlignmentArtifact.assignment_questions_digest == questions_digest,
        )
    )


async def register_success(
    session_factory: SessionFactory,
    *,
    artifact_id: UUID,
    submission_id: UUID,
    assignment_id: UUID,
    canonical_artifact_id: UUID,
    canonical_sha256: str,
    aligner_version: str,
    questions_digest: str,
    status: str,
    bucket: str,
    object_key: str,
    alignment_sha256: str,
    alignment_size_bytes: int,
    question_count: int,
    aligned_count: int,
    review_required_count: int,
    unmatched_count: int,
    not_observed_count: int,
) -> AnswerAlignmentArtifact:
    """Register once; target all unique conflicts to avoid deterministic-ID races."""

    statement = insert(AnswerAlignmentArtifact).values(
        id=artifact_id,
        submission_id=submission_id,
        assignment_id=assignment_id,
        canonical_artifact_id=canonical_artifact_id,
        canonical_sha256=canonical_sha256,
        aligner_version=aligner_version,
        assignment_questions_digest=questions_digest,
        status=status,
        bucket=bucket,
        object_key=object_key,
        alignment_sha256=alignment_sha256,
        alignment_size_bytes=alignment_size_bytes,
        question_count=question_count,
        aligned_count=aligned_count,
        review_required_count=review_required_count,
        unmatched_count=unmatched_count,
        not_observed_count=not_observed_count,
    ).on_conflict_do_nothing()

    async with session_factory() as session, session.begin():
        await session.execute(statement)
        record = await session.scalar(
            select(AnswerAlignmentArtifact).where(
                AnswerAlignmentArtifact.canonical_artifact_id == canonical_artifact_id,
                AnswerAlignmentArtifact.aligner_version == aligner_version,
                AnswerAlignmentArtifact.assignment_questions_digest == questions_digest,
            )
        )
        if record is None:
            raise RuntimeError("answer alignment registration did not produce an index")
        return record
