"""Private teacher-text versions and bounded Unicode chunk index."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base, utc_now


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("document_id", "source_version", name="uq_knowledge_document_version"),
        UniqueConstraint("document_id", "input_sha256", name="uq_knowledge_document_input"),
        CheckConstraint(
            "source_version > 0 AND size_bytes > 0 AND size_bytes <= 65536 "
            "AND chunk_count > 0 AND chunk_count <= 256",
            name="ck_knowledge_sizes",
        ),
        CheckConstraint(
            "length(source_sha256) = 64 AND length(input_sha256) = 64",
            name="ck_knowledge_document_hashes",
        ),
        CheckConstraint("status IN ('active','superseded','revoked')", name="ck_knowledge_status"),
        CheckConstraint(
            "source_type IN ('markdown','text') AND permission IN ('synthetic','authorized')",
            name="ck_knowledge_permission",
        ),
        CheckConstraint(
            "length(trim(title)) > 0 AND length(trim(bucket)) > 0 AND length(trim(object_key)) > 0",
            name="ck_knowledge_metadata",
        ),
        CheckConstraint(
            "(status='revoked') = (revoked_at IS NOT NULL)", name="ck_knowledge_revocation"
        ),
        Index(
            "uq_knowledge_active_document",
            "document_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_knowledge_assignment_status", "assignment_id", "status"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    assignment_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False
    )
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    permission: Mapped[str] = mapped_column(String(16), nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "chunk_index", name="uq_knowledge_chunk_index"),
        CheckConstraint(
            "chunk_index >= 0 AND text_start >= 0 AND text_end > text_start "
            "AND text_end-text_start = char_length(chunk_text) "
            "AND char_length(chunk_text) <= 400",
            name="ck_knowledge_chunk_range",
        ),
        CheckConstraint(
            "length(chunk_text_sha256) = 64 AND content_pointer = '/text'",
            name="ck_knowledge_chunk_source",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    document_version_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    text_start: Mapped[int] = mapped_column(Integer, nullable=False)
    text_end: Mapped[int] = mapped_column(Integer, nullable=False)
    content_pointer: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )
