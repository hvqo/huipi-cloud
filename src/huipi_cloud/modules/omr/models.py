"""Immutable templates, source-bound recognition, and teacher decisions."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base


class OmrTemplate(Base):
    __tablename__ = "omr_templates"
    __table_args__ = (
        UniqueConstraint("paper_version_id", name="uq_omr_paper_template"),
        UniqueConstraint("id", "paper_version_id", name="uq_omr_template_paper"),
        CheckConstraint(
            "length(content_sha256)=64 AND length(mapping_digest)=64", name="ck_omr_template_digest"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    paper_version_id: Mapped[UUID] = mapped_column(ForeignKey("paper_versions.id"))
    engine_version: Mapped[str] = mapped_column(String(40))
    content_sha256: Mapped[str] = mapped_column(String(64))
    mapping_digest: Mapped[str] = mapped_column(String(64))
    creation_txid: Mapped[int] = mapped_column(BigInteger, server_default=func.txid_current())
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OmrTemplateQuestion(Base):
    __tablename__ = "omr_template_questions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["template_id", "paper_version_id"],
            ["omr_templates.id", "omr_templates.paper_version_id"],
            name="fk_omr_question_template",
        ),
        ForeignKeyConstraint(
            ["mapping_id", "paper_version_id"],
            ["paper_question_mappings.id", "paper_question_mappings.paper_version_id"],
            name="fk_omr_question_paper",
        ),
        UniqueConstraint("template_id", "mapping_id", name="uq_omr_template_mapping"),
        CheckConstraint("ordinal BETWEEN 0 AND 29", name="ck_omr_question_order"),
    )
    template_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_version_id: Mapped[UUID] = mapped_column(Uuid)
    mapping_id: Mapped[UUID] = mapped_column(Uuid)
    printed_label: Mapped[str] = mapped_column(String(12))


class OmrRecognition(Base):
    __tablename__ = "omr_recognitions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["template_id", "paper_version_id"],
            ["omr_templates.id", "omr_templates.paper_version_id"],
            name="fk_omr_recognition_template",
        ),
        ForeignKeyConstraint(
            ["script_version_id", "script_id", "exam_session_id"],
            [
                "answer_script_versions.id",
                "answer_script_versions.answer_script_id",
                "answer_script_versions.exam_session_id",
            ],
            name="fk_omr_recognition_script",
        ),
        ForeignKeyConstraint(
            ["script_version_id", "scan_page_id"],
            ["answer_script_pages.version_id", "answer_script_pages.scan_page_id"],
            name="fk_omr_recognition_page",
        ),
        UniqueConstraint("script_version_id", "template_id", name="uq_omr_source_template"),
        UniqueConstraint("id", "template_id", name="uq_omr_recognition_template"),
        CheckConstraint(
            "length(manifest_sha256)=64 AND length(page_sha256)=64",
            name="ck_omr_recognition_digest",
        ),
        CheckConstraint("jsonb_typeof(results)='array'", name="ck_omr_recognition_results"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    template_id: Mapped[UUID] = mapped_column(Uuid)
    paper_version_id: Mapped[UUID] = mapped_column(Uuid)
    script_id: Mapped[UUID] = mapped_column(Uuid)
    script_version_id: Mapped[UUID] = mapped_column(Uuid)
    exam_session_id: Mapped[UUID] = mapped_column(Uuid)
    scan_page_id: Mapped[UUID] = mapped_column(Uuid)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    page_sha256: Mapped[str] = mapped_column(String(64))
    results: Mapped[list] = mapped_column(JSONB)
    created_by_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OmrDecision(Base):
    __tablename__ = "omr_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["recognition_id", "template_id"],
            ["omr_recognitions.id", "omr_recognitions.template_id"],
            name="fk_omr_decision_recognition",
        ),
        ForeignKeyConstraint(
            ["template_id", "mapping_id"],
            ["omr_template_questions.template_id", "omr_template_questions.mapping_id"],
            name="fk_omr_decision_question",
        ),
        UniqueConstraint(
            "recognition_id", "mapping_id", "revision", name="uq_omr_decision_revision"
        ),
        CheckConstraint(
            "revision>0 AND choice IN ('A','B','C','D','blank') AND points>=0",
            name="ck_omr_decision_value",
        ),
        CheckConstraint("length(trim(reason)) BETWEEN 1 AND 300", name="ck_omr_decision_reason"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    recognition_id: Mapped[UUID] = mapped_column(Uuid)
    template_id: Mapped[UUID] = mapped_column(Uuid)
    mapping_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    choice: Mapped[str] = mapped_column(String(8))
    points: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    reason: Mapped[str] = mapped_column(String(300))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OmrOperation(Base):
    __tablename__ = "omr_operations"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_omr_operation_request"),
        CheckConstraint(
            "length(payload_sha256)=64 AND jsonb_typeof(result)='object'",
            name="ck_omr_operation_digest",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    request_id: Mapped[UUID] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(String(24))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
