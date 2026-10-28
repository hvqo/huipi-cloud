"""Minimal task-bound indices; assessment protocols remain the existing P3 models."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
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
        UniqueConstraint("workspace_id", "visual_selection_id", name="uq_exam_visual_score"),
        CheckConstraint(
            "(answer_review_id IS NOT NULL AND visual_selection_id IS NULL) OR "
            "(answer_review_id IS NULL AND visual_selection_id IS NOT NULL)",
            name="ck_exam_score_source_mode",
        ),
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
    answer_review_id: Mapped[UUID | None] = mapped_column(ForeignKey("answer_review_decisions.id"))
    visual_selection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("exam_visual_selections.id")
    )
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


class ExamVisualSelection(Base):
    """A teacher's immutable selection, bound to an exact answer-script version."""

    __tablename__ = "exam_visual_selections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "revision", name="uq_exam_visual_selection_revision"),
        UniqueConstraint("id", "script_version_id", name="uq_exam_visual_selection_version"),
        CheckConstraint("revision BETWEEN 1 AND 100", name="ck_exam_visual_selection_revision"),
        CheckConstraint("length(regions_sha256)=64", name="ck_exam_visual_selection_sha"),
        CheckConstraint(
            "NOT active OR text_answer_review_id IS NULL", name="ck_exam_visual_selection_mode"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("exam_grading_workspaces.id"))
    script_version_id: Mapped[UUID] = mapped_column(ForeignKey("answer_script_versions.id"))
    revision: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean)
    text_answer_review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("answer_review_decisions.id")
    )
    regions_sha256: Mapped[str] = mapped_column(String(64))
    creation_txid: Mapped[int] = mapped_column(BigInteger, server_default=func.txid_current())
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamVisualRegion(Base):
    __tablename__ = "exam_visual_regions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["selection_id", "script_version_id"],
            ["exam_visual_selections.id", "exam_visual_selections.script_version_id"],
            name="fk_exam_visual_region_selection",
        ),
        ForeignKeyConstraint(
            ["script_version_id", "scan_page_id"],
            ["answer_script_pages.version_id", "answer_script_pages.scan_page_id"],
            name="fk_exam_visual_region_page",
        ),
        CheckConstraint("ordinal BETWEEN 0 AND 7", name="ck_exam_visual_region_ordinal"),
        CheckConstraint(
            "width BETWEEN 1 AND 4096 AND height BETWEEN 1 AND 4096 AND "
            "x0>=0 AND y0>=0 AND x1>x0 AND y1>y0 AND x1<=width AND y1<=height",
            name="ck_exam_visual_region_bounds",
        ),
        CheckConstraint(
            "length(page_sha256)=64 AND length(crop_sha256)=64",
            name="ck_exam_visual_region_sha",
        ),
    )
    selection_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    script_version_id: Mapped[UUID] = mapped_column(Uuid)
    scan_page_id: Mapped[UUID] = mapped_column(Uuid)
    page_sha256: Mapped[str] = mapped_column(String(64))
    crop_sha256: Mapped[str] = mapped_column(String(64))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    x0: Mapped[int] = mapped_column(Integer)
    y0: Mapped[int] = mapped_column(Integer)
    x1: Mapped[int] = mapped_column(Integer)
    y1: Mapped[int] = mapped_column(Integer)
