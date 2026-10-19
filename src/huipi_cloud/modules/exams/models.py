"""Single-school exam containers and append-preserving shared-paper relationships."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
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


class ExamEvent(Base):
    __tablename__ = "exam_events"
    __table_args__ = (
        CheckConstraint(
            "lifecycle IN ('draft','active','closed','cancelled')", name="ck_exam_event_state"
        ),
        CheckConstraint("revision>0", name="ck_exam_event_revision"),
        CheckConstraint("length(trim(title))>0", name="ck_exam_event_title"),
        CheckConstraint(
            "category IN ('midterm','final','monthly','other')", name="ck_exam_event_category"
        ),
        Index("ix_exam_events_term", "academic_term_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    academic_term_id: Mapped[UUID] = mapped_column(
        ForeignKey("academic_terms.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(16))
    organizer_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    lifecycle: Mapped[str] = mapped_column(String(16), server_default="draft")
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamSession(Base):
    __tablename__ = "exam_sessions"
    __table_args__ = (
        UniqueConstraint("exam_event_id", "subject_id", "starts_at", name="uq_exam_session_slot"),
        CheckConstraint(
            "lifecycle IN ('draft','ready','closed','cancelled')", name="ck_exam_session_state"
        ),
        CheckConstraint("revision>0", name="ck_exam_session_revision"),
        CheckConstraint("ends_at>starts_at", name="ck_exam_session_time"),
        CheckConstraint("paper_policy IN ('single','ab')", name="ck_exam_session_policy"),
        CheckConstraint("length(trim(title))>0", name="ck_exam_session_title"),
        Index("ix_exam_sessions_event", "exam_event_id"),
        Index("ix_exam_sessions_subject", "subject_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    exam_event_id: Mapped[UUID] = mapped_column(ForeignKey("exam_events.id", ondelete="RESTRICT"))
    subject_id: Mapped[UUID] = mapped_column(ForeignKey("subjects.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paper_policy: Mapped[str] = mapped_column(String(16), server_default="single")
    lifecycle: Mapped[str] = mapped_column(String(16), server_default="draft")
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionPaper(Base):
    __tablename__ = "session_papers"
    __table_args__ = (
        Index(
            "uq_session_paper_active_code",
            "exam_session_id",
            "variant_code",
            unique=True,
            postgresql_where=text("status='active'"),
        ),
        Index(
            "uq_session_paper_active_version",
            "exam_session_id",
            "paper_version_id",
            unique=True,
            postgresql_where=text("status='active'"),
        ),
        Index("ix_session_papers_version", "paper_version_id"),
        CheckConstraint("variant_code IN ('A','B')", name="ck_session_paper_code"),
        CheckConstraint(
            "(status='active' AND revoked_at IS NULL) OR "
            "(status='revoked' AND revoked_at IS NOT NULL)",
            name="ck_session_paper_state",
        ),
        CheckConstraint(
            "length(content_sha256)=64 AND length(mapping_digest)=64",
            name="ck_session_paper_digest",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    exam_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="RESTRICT")
    )
    paper_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("paper_versions.id", ondelete="RESTRICT")
    )
    variant_code: Mapped[str] = mapped_column(String(1))
    status: Mapped[str] = mapped_column(String(16), server_default="active")
    content_sha256: Mapped[str] = mapped_column(String(64))
    mapping_digest: Mapped[str] = mapped_column(String(64))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExamOperation(Base):
    __tablename__ = "exam_operations"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_exam_operation_request"),
        CheckConstraint("length(payload_sha256)=64", name="ck_exam_operation_digest"),
        CheckConstraint("jsonb_typeof(result)='object'", name="ck_exam_operation_result"),
        CheckConstraint(
            "action IN ('create_event','create_session','edit_event','edit_session',"
            "'event_state','session_state','bind','unbind','enable_operator')",
            name="ck_exam_operation_action",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid)
    request_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(32))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
