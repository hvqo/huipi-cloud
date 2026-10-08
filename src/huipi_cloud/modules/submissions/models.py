"""Database models for original submission files and pending parsing records."""

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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from huipi_cloud.infrastructure.database.base import Base, utc_now


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "student_ref",
            name="uq_submissions_assignment_student_ref",
        ),
        CheckConstraint("length(trim(student_ref)) > 0", name="ck_submissions_student_ref"),
        CheckConstraint("status = 'submitted'", name="ck_submissions_status"),
        Index("ix_submissions_assignment_created_at", "assignment_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    assignment_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("assignments.id", ondelete="CASCADE"),
        nullable=False,
    )
    student_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="submitted",
        server_default="submitted",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    assignment = relationship("Assignment")
    file: Mapped["SubmissionFile"] = relationship(
        back_populates="submission",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    parsing_task: Mapped["ParsingTask"] = relationship(
        back_populates="submission",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )


class SubmissionFile(Base):
    __tablename__ = "submission_files"
    __table_args__ = (
        UniqueConstraint("submission_id", name="uq_submission_files_submission_id"),
        UniqueConstraint("bucket", "object_key", name="uq_submission_files_bucket_object_key"),
        CheckConstraint("length(trim(bucket)) > 0", name="ck_submission_files_bucket"),
        CheckConstraint("length(trim(object_key)) > 0", name="ck_submission_files_object_key"),
        CheckConstraint("length(trim(original_filename)) > 0", name="ck_submission_files_filename"),
        CheckConstraint(
            "content_type IN ('application/pdf', 'image/jpeg', 'image/png')",
            name="ck_submission_files_content_type",
        ),
        CheckConstraint("size_bytes > 0", name="ck_submission_files_size_positive"),
        CheckConstraint("length(sha256) = 64", name="ck_submission_files_sha256_length"),
        Index("ix_submission_files_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    submission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
    )
    bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )

    submission: Mapped[Submission] = relationship(back_populates="file")


class ParsingTask(Base):
    __tablename__ = "parsing_tasks"
    __table_args__ = (
        UniqueConstraint("submission_id", name="uq_parsing_tasks_submission_id"),
        CheckConstraint("status = 'pending'", name="ck_parsing_tasks_status"),
        Index("ix_parsing_tasks_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    submission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    submission: Mapped[Submission] = relationship(back_populates="parsing_task")
