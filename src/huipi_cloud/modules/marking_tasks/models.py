from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base


class MarkingTask(Base):
    __tablename__ = "marking_tasks"
    __table_args__ = (
        UniqueConstraint("workspace_id", "generation", name="uq_marking_generation"),
        UniqueConstraint("supersedes_id", name="uq_marking_successor"),
        UniqueConstraint("review_id", name="uq_marking_final_review"),
        Index(
            "uq_marking_current", "workspace_id", unique=True, postgresql_where=text("is_current")
        ),
        Index("ix_marking_assignee", "assignee_user_id", "is_current"),
        CheckConstraint("generation>0 AND revision>0", name="ck_marking_revision"),
        CheckConstraint(
            "status IN ('assigned','in_progress','completed','cancelled','superseded')",
            name="ck_marking_status",
        ),
        CheckConstraint("is_current=(status<>'superseded')", name="ck_marking_current"),
        CheckConstraint(
            "(status='completed' AND review_id IS NOT NULL AND completed_at IS NOT NULL) "
            "OR (status='superseded') OR (status<>'completed' AND review_id IS NULL "
            "AND completed_at IS NULL)",
            name="ck_marking_result",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    exam_session_id: Mapped[UUID] = mapped_column(ForeignKey("exam_sessions.id"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("exam_grading_workspaces.id"))
    generation: Mapped[int] = mapped_column(Integer)
    supersedes_id: Mapped[UUID | None] = mapped_column(ForeignKey("marking_tasks.id"))
    assignee_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    assigned_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    status: Mapped[str] = mapped_column(String(24), server_default="assigned")
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_id: Mapped[UUID | None] = mapped_column(ForeignKey("grade_review_revisions.review_id"))
    reason: Mapped[str] = mapped_column(String(256))


class MarkingOperation(Base):
    __tablename__ = "marking_operations"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_marking_operation"),
        CheckConstraint("length(payload_sha256)=64", name="ck_marking_operation_digest"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    exam_session_id: Mapped[UUID] = mapped_column(ForeignKey("exam_sessions.id"))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(32))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
