"""PostgreSQL index for immutable, source-bound GradeInput objects."""

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


class GradeInputArtifact(Base):
    """Small lookup index; student text and snapshots stay in private S3."""

    __tablename__ = "grade_input_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "submission_id",
            "question_id",
            "request_id",
            name="uq_grade_inputs_submission_question_request",
        ),
        UniqueConstraint(
            "bucket",
            "object_key",
            name="uq_grade_inputs_bucket_object_key",
        ),
        CheckConstraint(
            "question_number > 0 AND review_revision > 0",
            name="ck_grade_inputs_numbers",
        ),
        CheckConstraint(
            "length(canonical_sha256) = 64 AND length(alignment_sha256) = 64 "
            "AND length(assignment_questions_digest) = 64 "
            "AND length(review_request_sha256) = 64 AND length(source_file_sha256) = 64 "
            "AND length(answer_key_sha256) = 64 AND length(rubric_sha256) = 64 "
            "AND length(input_sha256) = 64 AND length(object_sha256) = 64",
            name="ck_grade_inputs_digests",
        ),
        CheckConstraint(
            "length(trim(schema_version)) > 0 AND length(trim(bucket)) > 0 "
            "AND length(trim(object_key)) > 0 AND object_size_bytes > 0",
            name="ck_grade_inputs_storage_metadata",
        ),
        CheckConstraint(
            "(schema_version='visual-1.0' AND visual_selection_id IS NOT NULL "
            "AND review_decision_id IS NULL) OR (schema_version<>'visual-1.0' "
            "AND visual_selection_id IS NULL AND review_decision_id IS NOT NULL)",
            name="ck_grade_inputs_source_mode",
        ),
        Index("ix_grade_inputs_submission_created", "submission_id", "created_at"),
        Index("ix_grade_inputs_question_created", "question_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("submissions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    assignment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("assignments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    question_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("questions.id", ondelete="RESTRICT"),
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
    assignment_questions_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    review_decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("answer_review_decisions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    visual_selection_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("exam_visual_selections.id", ondelete="RESTRICT")
    )
    review_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    review_request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    answer_key_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("answer_keys.id", ondelete="RESTRICT"),
        nullable=False,
    )
    answer_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rubric_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )


class GradeProposalArtifact(Base):
    """Small index for a private immutable LLM scoring proposal."""

    __tablename__ = "grade_proposal_artifacts"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_grade_proposals_request_id"),
        UniqueConstraint("bucket", "object_key", name="uq_grade_proposals_bucket_object_key"),
        CheckConstraint(
            "length(grade_input_sha256) = 64 AND length(request_fingerprint) = 64 "
            "AND length(generation_config_digest) = 64 AND length(object_sha256) = 64",
            name="ck_grade_proposals_digests",
        ),
        CheckConstraint(
            "grading_status IN ('completed_candidate', 'grading_abstained') "
            "AND length(trim(provider)) > 0 AND length(trim(model_id)) > 0 "
            "AND length(trim(prompt_version)) > 0 AND length(trim(bucket)) > 0 "
            "AND length(trim(object_key)) > 0 AND object_size_bytes > 0 "
            "AND model_elapsed_ms >= 0",
            name="ck_grade_proposals_metadata",
        ),
        CheckConstraint(
            "(prompt_tokens IS NULL OR prompt_tokens >= 0) "
            "AND (completion_tokens IS NULL OR completion_tokens >= 0)",
            name="ck_grade_proposals_usage",
        ),
        Index("ix_grade_proposals_input_created", "grade_input_artifact_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    grade_input_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("grade_input_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    grade_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    submission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("submissions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    question_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("questions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    grading_status: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    model_elapsed_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
