"""Trusted teacher-class relationships and versioned homework submissions."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base


class TeachingAssignment(Base):
    __tablename__ = "homework_teaching_assignments"
    __table_args__ = (
        Index(
            "uq_homework_teaching_active",
            "teacher_id",
            "class_id",
            "term_id",
            "subject_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    teacher_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    class_id: Mapped[UUID] = mapped_column(
        ForeignKey("administrative_classes.id", ondelete="RESTRICT")
    )
    term_id: Mapped[UUID] = mapped_column(ForeignKey("academic_terms.id", ondelete="RESTRICT"))
    subject_id: Mapped[UUID] = mapped_column(ForeignKey("subjects.id", ondelete="RESTRICT"))
    granted_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


class HomeworkOffering(Base):
    __tablename__ = "homework_offerings"
    __table_args__ = (UniqueConstraint("assignment_id", name="uq_homework_offering_assignment"),)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    assignment_id: Mapped[UUID] = mapped_column(ForeignKey("assignments.id", ondelete="RESTRICT"))
    term_id: Mapped[UUID] = mapped_column(ForeignKey("academic_terms.id", ondelete="RESTRICT"))
    subject_id: Mapped[UUID] = mapped_column(ForeignKey("subjects.id", ondelete="RESTRICT"))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HomeworkClass(Base):
    __tablename__ = "homework_classes"
    __table_args__ = (
        UniqueConstraint("offering_id", "class_id", name="uq_homework_offering_class"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    offering_id: Mapped[UUID] = mapped_column(
        ForeignKey("homework_offerings.id", ondelete="RESTRICT")
    )
    class_id: Mapped[UUID] = mapped_column(
        ForeignKey("administrative_classes.id", ondelete="RESTRICT")
    )
    class_name: Mapped[str] = mapped_column(String(100))


class HomeworkParticipant(Base):
    __tablename__ = "homework_participants"
    __table_args__ = (
        UniqueConstraint("offering_id", "student_id", name="uq_homework_offering_student"),
        UniqueConstraint("id", "offering_id", name="uq_homework_participant_offering"),
        ForeignKeyConstraint(
            ["offering_id", "class_id"],
            ["homework_classes.offering_id", "homework_classes.class_id"],
            name="fk_homework_participant_class",
            ondelete="RESTRICT",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    offering_id: Mapped[UUID] = mapped_column(
        ForeignKey("homework_offerings.id", ondelete="RESTRICT")
    )
    student_id: Mapped[UUID] = mapped_column(ForeignKey("students.id", ondelete="RESTRICT"))
    enrollment_id: Mapped[UUID] = mapped_column(
        ForeignKey("class_enrollments.id", ondelete="RESTRICT")
    )
    class_id: Mapped[UUID] = mapped_column(
        ForeignKey("administrative_classes.id", ondelete="RESTRICT")
    )
    student_name: Mapped[str] = mapped_column(String(100))
    student_code: Mapped[str] = mapped_column(String(64))


class HomeworkAttempt(Base):
    __tablename__ = "homework_attempts"
    __table_args__ = (
        UniqueConstraint("participant_id", "version", name="uq_homework_attempt_version"),
        UniqueConstraint("actor_id", "request_id", name="uq_homework_attempt_request"),
        UniqueConstraint("submission_id", name="uq_homework_attempt_submission"),
        CheckConstraint(
            "version > 0 AND length(file_sha256) = 64", name="ck_homework_attempt_data"
        ),
        Index(
            "uq_homework_attempt_current",
            "participant_id",
            unique=True,
            postgresql_where=text("is_current"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("homework_participants.id", ondelete="RESTRICT")
    )
    submission_id: Mapped[UUID] = mapped_column(ForeignKey("submissions.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    is_current: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    file_sha256: Mapped[str] = mapped_column(String(64))
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
