"""PostgreSQL index for immutable Answer Alignment documents in private S3 storage."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base, utc_now


class AnswerAlignmentArtifact(Base):
    """One deterministic alignment result and the immutable object that stores it."""

    __tablename__ = "answer_alignment_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "canonical_artifact_id",
            "aligner_version",
            "assignment_questions_digest",
            name="uq_answer_alignments_canonical_aligner_questions",
        ),
        CheckConstraint(
            "status IN ('complete', 'review_required')",
            name="ck_answer_alignment_status",
        ),
        CheckConstraint(
            "length(canonical_sha256) = 64 AND length(alignment_sha256) = 64 "
            "AND length(assignment_questions_digest) = 64",
            name="ck_answer_alignment_digests",
        ),
        CheckConstraint(
            "length(trim(bucket)) > 0 AND length(trim(object_key)) > 0 "
            "AND length(trim(aligner_version)) > 0",
            name="ck_answer_alignment_storage_and_version",
        ),
        CheckConstraint(
            "alignment_size_bytes > 0 AND question_count >= 0 AND aligned_count >= 0 "
            "AND review_required_count >= 0 AND unmatched_count >= 0 "
            "AND not_observed_count >= 0",
            name="ck_answer_alignment_counts",
        ),
        Index("ix_answer_alignment_submission_created_at", "submission_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    submission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
    )
    assignment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("assignments.id", ondelete="CASCADE"),
        nullable=False,
    )
    canonical_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("canonical_artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    aligner_version: Mapped[str] = mapped_column(String(32), nullable=False)
    assignment_questions_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    alignment_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    alignment_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False)
    aligned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    review_required_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unmatched_count: Mapped[int] = mapped_column(Integer, nullable=False)
    not_observed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
