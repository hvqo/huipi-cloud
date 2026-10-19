"""Stable participation identities and immutable, session-bound roster snapshots."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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


class ExamParticipation(Base):
    __tablename__ = "exam_participations"
    __table_args__ = (
        UniqueConstraint("exam_session_id", "student_id", name="uq_participation_session_student"),
        UniqueConstraint("id", "exam_session_id", "student_id", name="uq_participation_identity"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    exam_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="RESTRICT")
    )
    student_id: Mapped[UUID] = mapped_column(ForeignKey("students.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RosterRevision(Base):
    __tablename__ = "roster_revisions"
    __table_args__ = (
        UniqueConstraint("exam_session_id", "version_no", name="uq_roster_version"),
        UniqueConstraint("id", "exam_session_id", name="uq_roster_session"),
        UniqueConstraint("previous_revision_id", name="uq_roster_successor"),
        ForeignKeyConstraint(
            ["previous_revision_id", "exam_session_id"],
            ["roster_revisions.id", "roster_revisions.exam_session_id"],
            name="fk_roster_previous_session",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version_no>0 AND revision>0", name="ck_roster_revision"),
        CheckConstraint("length(trim(reason))>0", name="ck_roster_reason"),
        CheckConstraint("status IN ('draft','frozen')", name="ck_roster_status"),
        CheckConstraint("NOT is_current OR status='frozen'", name="ck_roster_current"),
        CheckConstraint(
            "(status='draft' AND frozen_at IS NULL AND frozen_by_user_id IS NULL) OR "
            "(status='frozen' AND frozen_at IS NOT NULL AND frozen_by_user_id IS NOT NULL)",
            name="ck_roster_frozen",
        ),
        Index(
            "uq_roster_draft",
            "exam_session_id",
            unique=True,
            postgresql_where=text("status='draft'"),
        ),
        Index(
            "uq_roster_current", "exam_session_id", unique=True, postgresql_where=text("is_current")
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    exam_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="RESTRICT")
    )
    version_no: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    previous_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    business_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), server_default="draft")
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    frozen_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RosterEntrySnapshot(Base):
    __tablename__ = "roster_entry_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["roster_revision_id", "exam_session_id"],
            ["roster_revisions.id", "roster_revisions.exam_session_id"],
            name="fk_roster_entry_revision",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["participation_id", "exam_session_id", "student_id"],
            [
                "exam_participations.id",
                "exam_participations.exam_session_id",
                "exam_participations.student_id",
            ],
            name="fk_roster_entry_participation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["session_paper_id", "exam_session_id"],
            ["session_papers.id", "session_papers.exam_session_id"],
            name="fk_roster_entry_paper",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("roster_revision_id", "student_id", name="uq_roster_entry_student"),
        UniqueConstraint("roster_revision_id", "exam_number", name="uq_roster_entry_exam_number"),
        CheckConstraint(
            "exam_number IS NULL OR exam_number ~ '^[A-Z0-9_-]{1,32}$'",
            name="ck_roster_exam_number",
        ),
        CheckConstraint(
            "length(trim(student_name))>0 AND length(trim(student_code))>0 AND "
            "length(trim(class_name))>0 AND length(trim(class_code))>0",
            name="ck_roster_entry_labels",
        ),
        Index("ix_roster_entry_participation", "participation_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    roster_revision_id: Mapped[UUID] = mapped_column(Uuid)
    exam_session_id: Mapped[UUID] = mapped_column(Uuid)
    participation_id: Mapped[UUID] = mapped_column(Uuid)
    student_id: Mapped[UUID] = mapped_column(Uuid)
    enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("class_enrollments.id", ondelete="RESTRICT")
    )
    administrative_class_id: Mapped[UUID] = mapped_column(
        ForeignKey("administrative_classes.id", ondelete="RESTRICT")
    )
    student_name: Mapped[str] = mapped_column(String(100))
    student_code: Mapped[str] = mapped_column(String(64))
    class_name: Mapped[str] = mapped_column(String(100))
    class_code: Mapped[str] = mapped_column(String(32))
    exam_number: Mapped[str | None] = mapped_column(String(32))
    session_paper_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RosterOperation(Base):
    __tablename__ = "roster_operations"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_roster_operation_request"),
        CheckConstraint("length(payload_sha256)=64", name="ck_roster_operation_digest"),
        CheckConstraint("jsonb_typeof(result)='object'", name="ck_roster_operation_result"),
        CheckConstraint(
            "action IN ('create','replace','correct','freeze','enable_operator')",
            name="ck_roster_operation_action",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid)
    request_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(32))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
