"""Append-only PostgreSQL records for human answer-presence reviews."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base, utc_now


class AnswerReviewDecision(Base):
    """One immutable reviewer decision for one Submission/Question revision."""

    __tablename__ = "answer_review_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('response_present', 'response_absent', 'uncertain')",
            name="ck_answer_review_decision_value",
        ),
        CheckConstraint(
            "alignment_status IN ('aligned', 'review_required', 'not_observed')",
            name="ck_answer_review_alignment_status",
        ),
        CheckConstraint(
            "question_number > 0 AND revision > 0",
            name="ck_answer_review_numbers",
        ),
        CheckConstraint(
            "length(alignment_sha256) = 64 AND length(canonical_sha256) = 64 "
            "AND length(assignment_questions_digest) = 64 AND length(request_sha256) = 64",
            name="ck_answer_review_digests",
        ),
        CheckConstraint(
            "length(trim(aligner_version)) > 0 AND review_schema_version = '1.0' "
            "AND length(trim(reviewer_ref)) > 0",
            name="ck_answer_review_nonblank_metadata",
        ),
        CheckConstraint(
            "jsonb_typeof(response_regions) = 'array' "
            "AND jsonb_typeof(excluded_prompt_regions) = 'array' "
            "AND jsonb_typeof(uncertain_regions) = 'array' "
            "AND jsonb_typeof(reason_codes) = 'array'",
            name="ck_answer_review_json_arrays",
        ),
        CheckConstraint(
            "(decision = 'response_present' AND jsonb_array_length(response_regions) > 0 "
            "AND jsonb_array_length(uncertain_regions) = 0) OR "
            "(decision = 'response_absent' AND jsonb_array_length(response_regions) = 0 "
            "AND jsonb_array_length(uncertain_regions) = 0 "
            "AND jsonb_array_length(excluded_prompt_regions) > 0) OR "
            "(decision = 'uncertain' AND jsonb_array_length(response_regions) = 0 "
            "AND (jsonb_array_length(uncertain_regions) > 0 "
            "OR reason_codes @> '[\"source_quality_insufficient\"]'::jsonb "
            "OR reason_codes @> '[\"response_not_linked_to_question\"]'::jsonb))",
            name="ck_answer_review_decision_evidence",
        ),
        UniqueConstraint(
            "submission_id",
            "question_id",
            "revision",
            name="uq_answer_review_submission_question_revision",
        ),
        UniqueConstraint(
            "submission_id",
            "question_id",
            "request_id",
            name="uq_answer_review_submission_question_request",
        ),
        UniqueConstraint("supersedes_decision_id", name="uq_answer_review_successor"),
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
    question_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("questions.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_number: Mapped[int] = mapped_column(Integer, nullable=False)
    alignment_status: Mapped[str] = mapped_column(String(24), nullable=False)
    alignment_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("answer_alignment_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    alignment_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    aligner_version: Mapped[str] = mapped_column(String(32), nullable=False)
    assignment_questions_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_document_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("canonical_artifacts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    review_schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    response_regions: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    excluded_prompt_regions: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    uncertain_regions: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    reason_codes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    reviewer_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("answer_review_decisions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
