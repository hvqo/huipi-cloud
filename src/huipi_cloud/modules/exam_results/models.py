"""Class access narrows one existing Grant; no aggregate scores are persisted."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base


class ExamResultClassAccess(Base):
    __tablename__ = "exam_result_class_access"
    __table_args__ = (
        Index(
            "uq_result_class_access_active",
            "grant_id",
            "exam_session_id",
            "administrative_class_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    grant_id: Mapped[UUID] = mapped_column(ForeignKey("security_grants.id", ondelete="RESTRICT"))
    exam_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="RESTRICT")
    )
    administrative_class_id: Mapped[UUID] = mapped_column(
        ForeignKey("administrative_classes.id", ondelete="RESTRICT")
    )
    export_allowed: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExamAttendanceDecision(Base):
    """A teacher's explicit attendance fact, bound to a frozen roster entry."""

    __tablename__ = "exam_attendance_decisions"
    __table_args__ = (
        UniqueConstraint(
            "exam_session_id", "participation_id", "revision", name="uq_exam_attendance_revision"
        ),
        CheckConstraint("revision > 0", name="ck_exam_attendance_revision"),
        CheckConstraint(
            "status IN ('present','absent','unverified')", name="ck_exam_attendance_status"
        ),
        CheckConstraint("length(trim(reason)) BETWEEN 1 AND 300", name="ck_exam_attendance_reason"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    exam_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="RESTRICT")
    )
    roster_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("roster_revisions.id", ondelete="RESTRICT")
    )
    roster_entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("roster_entry_snapshots.id", ondelete="RESTRICT")
    )
    participation_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_participations.id", ondelete="RESTRICT")
    )
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(300))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamFinalVersion(Base):
    """One immutable score snapshot with a separately guarded approval lifecycle."""

    __tablename__ = "exam_final_versions"
    __table_args__ = (
        UniqueConstraint("exam_session_id", "version_no", name="uq_exam_final_version"),
        CheckConstraint("version_no>0 AND revision>0", name="ck_exam_final_revision"),
        CheckConstraint(
            "status IN ('pending','approved','published','cancelled')", name="ck_exam_final_status"
        ),
        CheckConstraint(
            "length(snapshot_sha256)=64 AND length(trim(reason)) BETWEEN 1 AND 300",
            name="ck_exam_final_digest",
        ),
        Index(
            "uq_exam_final_pending",
            "exam_session_id",
            unique=True,
            postgresql_where=text("status IN ('pending','approved')"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    exam_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="RESTRICT")
    )
    roster_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("roster_revisions.id", ondelete="RESTRICT")
    )
    version_no: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    supersedes_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("exam_final_versions.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(300))
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    approved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    published_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    creation_txid: Mapped[int] = mapped_column(BigInteger, server_default=func.txid_current())


class ExamFinalStudent(Base):
    __tablename__ = "exam_final_students"
    __table_args__ = (
        UniqueConstraint("version_id", "participation_id", name="uq_exam_final_student"),
        CheckConstraint("attendance IN ('present','absent')", name="ck_exam_final_attendance"),
        CheckConstraint(
            "(attendance='absent' AND total IS NULL AND script_version_id IS NULL) OR "
            "(attendance='present' AND total IS NOT NULL AND script_version_id IS NOT NULL "
            "AND total>=0 AND max_points>0 AND total<=max_points)",
            name="ck_exam_final_student_score",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    version_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_final_versions.id", ondelete="RESTRICT")
    )
    participation_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_participations.id", ondelete="RESTRICT")
    )
    roster_entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("roster_entry_snapshots.id", ondelete="RESTRICT")
    )
    attendance_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_attendance_decisions.id", ondelete="RESTRICT")
    )
    class_id: Mapped[UUID] = mapped_column(
        ForeignKey("administrative_classes.id", ondelete="RESTRICT")
    )
    script_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("answer_script_versions.id", ondelete="RESTRICT")
    )
    session_paper_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("session_papers.id", ondelete="RESTRICT")
    )
    paper_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("paper_versions.id", ondelete="RESTRICT")
    )
    attendance: Mapped[str] = mapped_column(String(16))
    total: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    max_points: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))


class ExamFinalQuestion(Base):
    __tablename__ = "exam_final_questions"
    __table_args__ = (
        UniqueConstraint("student_result_id", "mapping_id", name="uq_exam_final_question"),
        CheckConstraint(
            "points>=0 AND max_points>0 AND points<=max_points", name="ck_exam_final_question_score"
        ),
        CheckConstraint(
            "(review_id IS NOT NULL AND omr_decision_id IS NULL) OR "
            "(review_id IS NULL AND omr_decision_id IS NOT NULL)",
            name="ck_exam_final_question_evidence",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    student_result_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_final_students.id", ondelete="RESTRICT")
    )
    mapping_id: Mapped[UUID] = mapped_column(
        ForeignKey("paper_question_mappings.id", ondelete="RESTRICT")
    )
    review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("grade_review_revisions.review_id", ondelete="RESTRICT")
    )
    omr_decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("omr_decisions.id", ondelete="RESTRICT")
    )
    question_number: Mapped[int] = mapped_column(Integer)
    points: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    max_points: Mapped[Decimal] = mapped_column(Numeric(8, 2))


class ExamFinalOperation(Base):
    __tablename__ = "exam_final_operations"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_exam_final_operation"),
        CheckConstraint("length(payload_sha256)=64", name="ck_exam_final_operation_sha"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    exam_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="RESTRICT")
    )
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(32))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
