"""Minimal task-bound indices; assessment protocols remain the existing P3 models."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base


class ExamGradingWorkspace(Base):
    __tablename__ = "exam_grading_workspaces"
    __table_args__ = (
        UniqueConstraint(
            "parsing_job_id", "paper_question_mapping_id", name="uq_exam_workspace_source"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    parsing_job_id: Mapped[UUID] = mapped_column(ForeignKey("exam_parsing_jobs.id"))
    paper_question_mapping_id: Mapped[UUID] = mapped_column(
        ForeignKey("paper_question_mappings.id")
    )
    alignment_artifact_id: Mapped[UUID] = mapped_column(ForeignKey("answer_alignment_artifacts.id"))
    question_id: Mapped[UUID] = mapped_column(ForeignKey("questions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamScoringJob(Base):
    __tablename__ = "exam_scoring_jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "answer_review_id", name="uq_exam_score_review"),
        UniqueConstraint("grade_input_id", name="uq_exam_score_input"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','invalidated')",
            name="ck_exam_score_status",
        ),
        CheckConstraint(
            "(status='running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status<>'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_exam_score_lease",
        ),
        CheckConstraint(
            "(status='succeeded')=(proposal_id IS NOT NULL)", name="ck_exam_score_result"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("exam_grading_workspaces.id"))
    answer_review_id: Mapped[UUID] = mapped_column(ForeignKey("answer_review_decisions.id"))
    grade_input_id: Mapped[UUID] = mapped_column(ForeignKey("grade_input_artifacts.id"))
    requested_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    proposal_id: Mapped[UUID | None] = mapped_column(ForeignKey("grade_proposal_artifacts.id"))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamGradingOperation(Base):
    __tablename__ = "exam_grading_operations"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_exam_grading_request"),
        CheckConstraint("length(payload_sha256)=64", name="ck_exam_grading_digest"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    parsing_job_id: Mapped[UUID] = mapped_column(ForeignKey("exam_parsing_jobs.id"))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(32))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
