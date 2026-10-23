"""Only the root pointer and current allocations change; all source facts are immutable."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
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


class AnswerScript(Base):
    __tablename__ = "answer_scripts"
    __table_args__ = (
        UniqueConstraint("id", "exam_session_id", name="uq_script_session"),
        UniqueConstraint("current_participation_id", name="uq_script_current_participation"),
        ForeignKeyConstraint(
            ["current_version_id", "id", "exam_session_id"],
            [
                "answer_script_versions.id",
                "answer_script_versions.answer_script_id",
                "answer_script_versions.exam_session_id",
            ],
            name="fk_script_current",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint("revision>0", name="ck_script_revision"),
        Index("ix_script_session", "exam_session_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    exam_session_id: Mapped[UUID] = mapped_column(
        ForeignKey("exam_sessions.id", ondelete="RESTRICT")
    )
    current_version_id: Mapped[UUID] = mapped_column(Uuid)
    current_participation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("exam_participations.id", ondelete="RESTRICT")
    )
    revision: Mapped[int] = mapped_column(Integer)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnswerScriptVersion(Base):
    __tablename__ = "answer_script_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["answer_script_id", "exam_session_id"],
            ["answer_scripts.id", "answer_scripts.exam_session_id"],
            name="fk_script_version_session",
        ),
        ForeignKeyConstraint(
            ["previous_version_id", "answer_script_id", "exam_session_id"],
            [
                "answer_script_versions.id",
                "answer_script_versions.answer_script_id",
                "answer_script_versions.exam_session_id",
            ],
            name="fk_script_previous",
        ),
        ForeignKeyConstraint(
            ["roster_revision_id", "exam_session_id"],
            ["roster_revisions.id", "roster_revisions.exam_session_id"],
            name="fk_script_roster_session",
        ),
        ForeignKeyConstraint(
            ["session_paper_id", "exam_session_id"],
            ["session_papers.id", "session_papers.exam_session_id"],
            name="fk_script_paper_session",
        ),
        UniqueConstraint(
            "id", "answer_script_id", "exam_session_id", name="uq_script_version_identity"
        ),
        UniqueConstraint("answer_script_id", "version_no", name="uq_script_version_number"),
        UniqueConstraint("previous_version_id", name="uq_script_successor"),
        CheckConstraint("version_no BETWEEN 1 AND 100", name="ck_script_version_limit"),
        CheckConstraint("status IN ('draft','confirmed','revoked')", name="ck_script_status"),
        CheckConstraint("completeness IN ('complete','incomplete')", name="ck_script_completeness"),
        CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 300 AND length(manifest_sha256)=64",
            name="ck_script_reason_digest",
        ),
        CheckConstraint(
            "(status='confirmed' AND roster_revision_id IS NOT NULL AND roster_entry_id IS "
            "NOT NULL AND participation_id IS NOT NULL AND session_paper_id IS NOT NULL) OR "
            "(status<>'confirmed' AND roster_revision_id IS NULL AND roster_entry_id IS NULL "
            "AND participation_id IS NULL AND session_paper_id IS NULL)",
            name="ck_script_confirmed_identity",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    answer_script_id: Mapped[UUID] = mapped_column(Uuid)
    exam_session_id: Mapped[UUID] = mapped_column(Uuid)
    version_no: Mapped[int] = mapped_column(Integer)
    previous_version_id: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(16))
    completeness: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(300))
    creation_txid: Mapped[int] = mapped_column(BigInteger, server_default=func.txid_current())
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    roster_revision_id: Mapped[UUID | None] = mapped_column(Uuid)
    roster_entry_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("roster_entry_snapshots.id", ondelete="RESTRICT")
    )
    participation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("exam_participations.id", ondelete="RESTRICT")
    )
    session_paper_id: Mapped[UUID | None] = mapped_column(Uuid)
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnswerScriptPage(Base):
    __tablename__ = "answer_script_pages"
    __table_args__ = (
        UniqueConstraint("version_id", "scan_page_id", name="uq_script_version_page"),
        CheckConstraint("ordinal BETWEEN 0 AND 99", name="ck_script_page_ordinal"),
        CheckConstraint(
            "source_page_index BETWEEN 0 AND 49 AND length(page_sha256)=64 AND "
            "length(file_sha256)=64",
            name="ck_script_page_source",
        ),
    )
    version_id: Mapped[UUID] = mapped_column(
        ForeignKey("answer_script_versions.id", ondelete="RESTRICT"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    scan_page_id: Mapped[UUID] = mapped_column(ForeignKey("scan_pages.id", ondelete="RESTRICT"))
    file_id: Mapped[UUID] = mapped_column(ForeignKey("scan_files.id", ondelete="RESTRICT"))
    source_page_index: Mapped[int] = mapped_column(Integer)
    page_sha256: Mapped[str] = mapped_column(String(64))
    file_sha256: Mapped[str] = mapped_column(String(64))


class CurrentPageAllocation(Base):
    __tablename__ = "current_page_allocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id", "scan_page_id"],
            ["answer_script_pages.version_id", "answer_script_pages.scan_page_id"],
            name="fk_allocation_page",
        ),
        ForeignKeyConstraint(
            ["version_id", "answer_script_id", "exam_session_id"],
            [
                "answer_script_versions.id",
                "answer_script_versions.answer_script_id",
                "answer_script_versions.exam_session_id",
            ],
            name="fk_allocation_version",
        ),
        Index("ix_allocation_script", "answer_script_id"),
    )
    scan_page_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    answer_script_id: Mapped[UUID] = mapped_column(Uuid)
    version_id: Mapped[UUID] = mapped_column(Uuid)
    exam_session_id: Mapped[UUID] = mapped_column(Uuid)


class AnswerScriptOperation(Base):
    __tablename__ = "answer_script_operations"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_script_request"),
        CheckConstraint("length(payload_sha256)=64", name="ck_script_operation_digest"),
        CheckConstraint(
            "action IN ('create','save_draft','confirm','revoke','enable_operator')",
            name="ck_script_operation_action",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(24))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
