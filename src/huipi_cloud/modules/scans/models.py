"""Stable session-bound source files/pages, no inferred student participation."""

from datetime import datetime
from uuid import UUID, uuid4

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


class ScanBatch(Base):
    __tablename__ = "scan_batches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["roster_revision_id", "exam_session_id"],
            ["roster_revisions.id", "roster_revisions.exam_session_id"],
            name="fk_scan_batch_roster_session",
            ondelete="RESTRICT",
        ),
        CheckConstraint("security_classification='exam_protected'", name="ck_scan_classification"),
        CheckConstraint("length(trim(title))>0", name="ck_scan_batch_title"),
        Index("ix_scan_batch_session", "exam_session_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    exam_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="RESTRICT")
    )
    roster_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(String(120))
    security_classification: Mapped[str] = mapped_column(
        String(24), server_default="exam_protected"
    )
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScanFile(Base):
    __tablename__ = "scan_files"
    __table_args__ = (
        UniqueConstraint("uploaded_by_user_id", "request_id", name="uq_scan_upload_request"),
        UniqueConstraint("batch_id", "sha256", name="uq_scan_batch_content"),
        CheckConstraint("status IN ('receiving','stored','failed')", name="ck_scan_file_status"),
        CheckConstraint("size_bytes>0 AND size_bytes<=33554432", name="ck_scan_file_size"),
        CheckConstraint(
            "page_count IS NULL OR page_count BETWEEN 1 AND 50", name="ck_scan_page_count"
        ),
        CheckConstraint(
            "content_type='application/pdf' AND source='manual_pdf'", name="ck_scan_source_type"
        ),
        CheckConstraint("length(sha256)=64 AND length(payload_sha256)=64", name="ck_scan_file_sha"),
        CheckConstraint("attempt_number>0", name="ck_scan_attempt_number"),
        CheckConstraint(
            "status<>'stored' OR (page_count IS NOT NULL AND failure_code IS NULL)",
            name="ck_scan_stored_complete",
        ),
        Index("ix_scan_file_batch", "batch_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    batch_id: Mapped[UUID] = mapped_column(ForeignKey("scan_batches.id", ondelete="RESTRICT"))
    uploaded_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(64), server_default="application/pdf")
    source: Mapped[str] = mapped_column(String(24), server_default="manual_pdf")
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    bucket: Mapped[str] = mapped_column(String(128))
    object_key: Mapped[str] = mapped_column(String(700))
    status: Mapped[str] = mapped_column(String(16), server_default="receiving")
    attempt_token: Mapped[UUID] = mapped_column(Uuid)
    attempt_number: Mapped[int] = mapped_column(Integer, server_default="1")
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    page_count: Mapped[int | None] = mapped_column(Integer)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScanPage(Base):
    __tablename__ = "scan_pages"
    __table_args__ = (
        UniqueConstraint("file_id", "page_index", name="uq_scan_page_index"),
        CheckConstraint("page_index BETWEEN 0 AND 49", name="ck_scan_page_index"),
        CheckConstraint("identity_status='unconfirmed'", name="ck_scan_page_identity"),
        CheckConstraint(
            "width>0 AND width<=1600 AND height>0 AND height<=1600",
            name="ck_scan_preview_dimensions",
        ),
        CheckConstraint(
            "size_bytes>0 AND size_bytes<=2097152 AND length(sha256)=64",
            name="ck_scan_preview_size",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    file_id: Mapped[UUID] = mapped_column(ForeignKey("scan_files.id", ondelete="RESTRICT"))
    page_index: Mapped[int] = mapped_column(Integer)
    identity_status: Mapped[str] = mapped_column(String(24), server_default="unconfirmed")
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(700))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScanOperation(Base):
    __tablename__ = "scan_operations"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id", "request_id", "action", "attempt_number", name="uq_scan_operation"
        ),
        CheckConstraint(
            "action IN ('batch_create','upload_begin','upload_stored','upload_failed',"
            "'enable_operator')",
            name="ck_scan_operation_action",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(32))
    attempt_number: Mapped[int] = mapped_column(Integer, server_default="0")
    payload_sha256: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
