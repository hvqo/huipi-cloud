"""Shared content versions over dedicated protected Assignment carriers."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base


class PaperFamily(Base):
    __tablename__ = "paper_families"
    __table_args__ = (
        UniqueConstraint("creation_key_sha256", name="uq_paper_family_creation"),
        CheckConstraint("length(creation_key_sha256)=64", name="ck_paper_family_digest"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    creation_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaperVersion(Base):
    __tablename__ = "paper_versions"
    __table_args__ = (
        UniqueConstraint("paper_family_id", "version_no", name="uq_paper_family_version"),
        UniqueConstraint("canonical_assignment_id", name="uq_paper_content_carrier"),
        UniqueConstraint("id", "canonical_assignment_id", name="uq_paper_carrier_pair"),
        UniqueConstraint("id", "paper_family_id", name="uq_paper_family_pair"),
        UniqueConstraint("prior_version_id", name="uq_paper_linear_successor"),
        ForeignKeyConstraint(
            ["prior_version_id", "paper_family_id"],
            ["paper_versions.id", "paper_versions.paper_family_id"],
            name="fk_paper_prior_family",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version_no>0 AND revision>0", name="ck_paper_numbers"),
        CheckConstraint("status IN ('draft','locked','retired')", name="ck_paper_status"),
        CheckConstraint("length(trim(title))>0", name="ck_paper_title"),
        CheckConstraint(
            "(status='draft' AND locked_at IS NULL) OR "
            "(status='locked' AND locked_at IS NOT NULL) OR status='retired'",
            name="ck_paper_lock_state",
        ),
        CheckConstraint(
            "length(source_content_sha256)=64 AND length(content_sha256)=64 AND "
            "length(question_digest)=64 AND length(rubric_digest)=64 AND length(mapping_digest)=64",
            name="ck_paper_digests",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    paper_family_id: Mapped[UUID] = mapped_column(ForeignKey("paper_families.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    canonical_assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("assignments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("assignments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    prior_version_id: Mapped[UUID | None] = mapped_column(Uuid)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    question_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    rubric_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaperQuestionMapping(Base):
    __tablename__ = "paper_question_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["paper_version_id", "canonical_assignment_id"],
            ["paper_versions.id", "paper_versions.canonical_assignment_id"],
            name="fk_mapping_paper_carrier",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["legacy_question_id", "canonical_assignment_id"],
            ["questions.id", "questions.assignment_id"],
            name="fk_mapping_question_carrier",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("paper_version_id", "question_key", name="uq_mapping_question_key"),
        UniqueConstraint("paper_version_id", "legacy_question_id", name="uq_mapping_question"),
        UniqueConstraint("paper_version_id", "sort_order", name="uq_mapping_order"),
        UniqueConstraint("paper_version_id", "printed_label", name="uq_mapping_label"),
        CheckConstraint("sort_order>0 AND length(trim(printed_label))>0", name="ck_mapping_order"),
        CheckConstraint("length(source_rubric_sha256)=64", name="ck_mapping_source_digest"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    paper_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    canonical_assignment_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    question_key: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    legacy_question_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    printed_label: Mapped[str] = mapped_column(String(80), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    source_question_id: Mapped[UUID] = mapped_column(ForeignKey("questions.id"), nullable=False)
    source_rubric_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prior_mapping_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("paper_question_mappings.id", ondelete="RESTRICT"),
    )


class PaperOperation(Base):
    __tablename__ = "paper_operations"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_paper_request"),
        CheckConstraint("length(payload_sha256)=64", name="ck_paper_operation_sha"),
        CheckConstraint("jsonb_typeof(result)='object'", name="ck_paper_operation_result"),
        CheckConstraint(
            "action IN "
            "('snapshot','fork','edit_question','reorder','lock','retire','enable_operator')",
            name="ck_paper_operation_action",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
