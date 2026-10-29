"""Teacher-selected visual evidence and bounded scoring runs for homework."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base


class HomeworkVisualSelection(Base):
    __tablename__ = "homework_visual_selections"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "question_id", "revision", name="uq_homework_visual_selection_revision"
        ),
        UniqueConstraint(
            "actor_user_id", "request_id", name="uq_homework_visual_selection_request"
        ),
        CheckConstraint(
            "revision BETWEEN 1 AND 100 AND length(regions_sha256)=64 "
            "AND length(request_sha256)=64",
            name="ck_homework_visual_selection_data",
        ),
        CheckConstraint(
            "jsonb_typeof(regions)='array' AND jsonb_array_length(regions) BETWEEN 1 AND 8",
            name="ck_homework_visual_selection_regions",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("homework_attempts.id", ondelete="RESTRICT")
    )
    submission_id: Mapped[UUID] = mapped_column(ForeignKey("submissions.id", ondelete="RESTRICT"))
    file_id: Mapped[UUID] = mapped_column(ForeignKey("submission_files.id", ondelete="RESTRICT"))
    question_id: Mapped[UUID] = mapped_column(ForeignKey("questions.id", ondelete="RESTRICT"))
    revision: Mapped[int] = mapped_column(Integer)
    regions: Mapped[list[dict]] = mapped_column(JSONB)
    regions_sha256: Mapped[str] = mapped_column(String(64))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    request_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HomeworkVisualJob(Base):
    __tablename__ = "homework_visual_jobs"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_homework_visual_job_request"),
        UniqueConstraint("selection_id", name="uq_homework_visual_job_selection"),
        CheckConstraint(
            "status IN ('running','succeeded','failed','invalidated')",
            name="ck_homework_visual_job_status",
        ),
        CheckConstraint("length(request_sha256)=64", name="ck_homework_visual_job_digest"),
        CheckConstraint(
            "(status='succeeded')=(proposal_id IS NOT NULL)", name="ck_homework_visual_job_result"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    selection_id: Mapped[UUID] = mapped_column(
        ForeignKey("homework_visual_selections.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    request_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    proposal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("grade_proposal_artifacts.id", ondelete="RESTRICT")
    )
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
