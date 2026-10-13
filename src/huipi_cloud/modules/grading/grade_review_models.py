"""Append-only PostgreSQL audit revisions for human grading-proposal reviews."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base, utc_now


class GradeReviewRevision(Base):
    """One immutable teacher-declared review revision for an existing proposal."""

    __tablename__ = "grade_review_revisions"
    __table_args__ = (
        CheckConstraint(
            "review_status IN ('accepted', 'revised', 'rejected', 'regrading_required')",
            name="ck_grade_review_status",
        ),
        CheckConstraint("review_revision > 0", name="ck_grade_review_revision_positive"),
        CheckConstraint(
            "review_schema_version = '1.0' AND length(trim(reviewer_ref)) > 0 "
            "AND length(proposal_sha256) = 64 AND length(grade_input_sha256) = 64 "
            "AND length(request_sha256) = 64",
            name="ck_grade_review_metadata",
        ),
        CheckConstraint(
            "jsonb_typeof(criterion_reviews) = 'array' "
            "AND jsonb_typeof(review_reason_codes) = 'array'",
            name="ck_grade_review_json_arrays",
        ),
        CheckConstraint(
            "((review_status IN ('accepted', 'revised')) "
            "AND jsonb_array_length(criterion_reviews) > 0 "
            "AND proposed_total_score IS NOT NULL AND reviewed_total_score IS NOT NULL) "
            "OR ((review_status IN ('rejected', 'regrading_required')) "
            "AND jsonb_array_length(criterion_reviews) = 0 "
            "AND reviewed_total_score IS NULL)",
            name="ck_grade_review_score_shape",
        ),
        UniqueConstraint("proposal_id", "review_revision", name="uq_grade_review_revision"),
        UniqueConstraint("proposal_id", "request_id", name="uq_grade_review_request"),
        UniqueConstraint("supersedes_review_id", name="uq_grade_review_successor"),
        Index("ix_grade_review_proposal_created", "proposal_id", "created_at"),
    )

    review_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("grade_proposal_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    grade_input_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("grade_input_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    proposal_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    grade_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    submission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("submissions.id", ondelete="RESTRICT"), nullable=False
    )
    question_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("questions.id", ondelete="RESTRICT"), nullable=False
    )
    review_schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    review_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_review_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("grade_review_revisions.review_id", ondelete="RESTRICT"),
        nullable=True,
    )
    reviewer_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    review_status: Mapped[str] = mapped_column(String(24), nullable=False)
    criterion_reviews: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    review_reason_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    review_comment: Mapped[str | None] = mapped_column(Text)
    proposed_total_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2, asdecimal=True))
    reviewed_total_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 2, asdecimal=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
