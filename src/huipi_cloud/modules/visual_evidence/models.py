"""Light PostgreSQL indexes for immutable visual proposal JSON objects."""

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


class VisualEvidenceArtifact(Base):
    """Index for one immutable, version-bound model proposal."""

    __tablename__ = "visual_evidence_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "submission_id",
            "question_id",
            "request_id",
            name="uq_visual_evidence_submission_question_request",
        ),
        UniqueConstraint(
            "proposal_bucket",
            "proposal_object_key",
            name="uq_visual_evidence_bucket_object_key",
        ),
        CheckConstraint(
            "outcome IN ('candidate_response_present', 'candidate_prompt_only', 'uncertain')",
            name="ck_visual_evidence_outcome",
        ),
        CheckConstraint("question_number > 0", name="ck_visual_evidence_question_number"),
        CheckConstraint(
            "length(canonical_sha256) = 64 AND length(alignment_sha256) = 64 "
            "AND length(original_file_sha256) = 64 AND length(model_input_digest) = 64 "
            "AND length(proposal_sha256) = 64",
            name="ck_visual_evidence_digests",
        ),
        CheckConstraint(
            "proposal_size_bytes > 0 AND length(trim(proposal_bucket)) > 0 "
            "AND length(trim(proposal_object_key)) > 0 AND length(trim(model_id)) > 0",
            name="ck_visual_evidence_storage_and_model",
        ),
        Index("ix_visual_evidence_submission_created_at", "submission_id", "created_at"),
        Index("ix_visual_evidence_question_created_at", "question_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
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
    question_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_number: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("canonical_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    alignment_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("answer_alignment_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    alignment_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    preprocessing_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    proposal_bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    proposal_object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    proposal_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
