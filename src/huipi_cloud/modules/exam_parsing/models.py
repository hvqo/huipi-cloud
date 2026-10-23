"""Independent leases plus immutable links to existing protected parser artifacts."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base


class ExamParsingJob(Base):
    __tablename__ = "exam_parsing_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["answer_script_version_id", "answer_script_id", "exam_session_id"],
            [
                "answer_script_versions.id",
                "answer_script_versions.answer_script_id",
                "answer_script_versions.exam_session_id",
            ],
            name="fk_exam_job_source",
        ),
        UniqueConstraint("answer_script_version_id", name="uq_exam_job_source"),
        CheckConstraint(
            "status IN ('pending','running','retry_wait','succeeded','failed','invalidated')",
            name="ck_exam_job_status",
        ),
        CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_exam_job_attempts"),
        CheckConstraint(
            "(status='running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) "
            "OR (status<>'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_exam_job_lease",
        ),
        CheckConstraint(
            "(status='succeeded' AND parsed_artifact_id IS NOT NULL AND "
            "canonical_artifact_id IS NOT NULL) OR (status<>'succeeded' AND "
            "parsed_artifact_id IS NULL AND canonical_artifact_id IS NULL)",
            name="ck_exam_job_result",
        ),
        Index("ix_exam_job_queue", "status", "next_run_at", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    answer_script_id: Mapped[UUID] = mapped_column(Uuid)
    answer_script_version_id: Mapped[UUID] = mapped_column(Uuid)
    exam_session_id: Mapped[UUID] = mapped_column(ForeignKey("exam_sessions.id"))
    requested_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    parsed_artifact_id: Mapped[UUID | None] = mapped_column(ForeignKey("parsed_artifacts.id"))
    canonical_artifact_id: Mapped[UUID | None] = mapped_column(ForeignKey("canonical_artifacts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamSubmissionLink(Base):
    __tablename__ = "exam_submission_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["answer_script_version_id", "answer_script_id", "exam_session_id"],
            [
                "answer_script_versions.id",
                "answer_script_versions.answer_script_id",
                "answer_script_versions.exam_session_id",
            ],
            name="fk_exam_link_source",
        ),
        UniqueConstraint("answer_script_version_id", name="uq_exam_link_source"),
        CheckConstraint("length(manifest_sha256)=64", name="ck_exam_link_manifest"),
    )
    job_id: Mapped[UUID] = mapped_column(ForeignKey("exam_parsing_jobs.id"), primary_key=True)
    answer_script_id: Mapped[UUID] = mapped_column(Uuid)
    answer_script_version_id: Mapped[UUID] = mapped_column(Uuid)
    exam_session_id: Mapped[UUID] = mapped_column(ForeignKey("exam_sessions.id"))
    roster_revision_id: Mapped[UUID] = mapped_column(ForeignKey("roster_revisions.id"))
    roster_entry_id: Mapped[UUID] = mapped_column(ForeignKey("roster_entry_snapshots.id"))
    participation_id: Mapped[UUID] = mapped_column(ForeignKey("exam_participations.id"))
    session_paper_id: Mapped[UUID] = mapped_column(ForeignKey("session_papers.id"))
    paper_version_id: Mapped[UUID] = mapped_column(ForeignKey("paper_versions.id"))
    submission_id: Mapped[UUID] = mapped_column(ForeignKey("submissions.id"), unique=True)
    submission_file_id: Mapped[UUID] = mapped_column(ForeignKey("submission_files.id"), unique=True)
    parsing_task_id: Mapped[UUID] = mapped_column(ForeignKey("parsing_tasks.id"), unique=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExamParsingOperation(Base):
    __tablename__ = "exam_parsing_operations"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_exam_parse_request"),
        CheckConstraint("length(payload_sha256)=64", name="ck_exam_parse_audit_digest"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("exam_parsing_jobs.id"))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(32))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
