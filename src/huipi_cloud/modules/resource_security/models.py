"""Small persisted catalog; no fictional school/exam UUID scopes."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base


class SecurityInstallation(Base):
    __tablename__ = "security_installations"
    __table_args__ = (CheckConstraint("id = 1", name="ck_security_single_installation"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)


class PermissionDefinition(Base):
    __tablename__ = "permission_definitions"
    code: Mapped[str] = mapped_column(String(80), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scope_kinds: Mapped[list[str]] = mapped_column(ARRAY(String(24)), nullable=False)


class SecurityScope(Base):
    __tablename__ = "security_scopes"
    __table_args__ = (
        CheckConstraint(
            "(kind='installation' AND installation_id=1 AND assignment_id IS NULL "
            "AND paper_version_id IS NULL AND exam_event_id IS NULL AND exam_s"
            "ession_id IS NULL) OR "
            "(kind='assignment' AND installation_id IS NULL AND assignment_id IS NOT NULL "
            "AND paper_version_id IS NULL AND exam_event_id IS NULL AND exam_s"
            "ession_id IS NULL) OR "
            "(kind='paper_version' AND installation_id IS NULL AND assignment_id IS NULL "
            "AND paper_version_id IS NOT NULL AND exam_event_id IS NULL AND ex"
            "am_session_id IS NULL) OR "
            "(kind='exam_event' AND installation_id IS NULL AND assignment_id IS NULL "
            "AND paper_version_id IS NULL AND exam_event_id IS NOT NULL AND ex"
            "am_session_id IS NULL) OR "
            "(kind='exam_session' AND installation_id IS NULL AND assignment_id IS NULL "
            "AND paper_version_id IS NULL AND exam_event_id IS NULL AND exam_s"
            "ession_id IS NOT NULL)",
            name="ck_security_scope_shape",
        ),
        UniqueConstraint("exam_event_id", name="uq_security_scope_event"),
        UniqueConstraint("exam_session_id", name="uq_security_scope_session"),
        UniqueConstraint("assignment_id", name="uq_security_scope_assignment"),
        UniqueConstraint("paper_version_id", name="uq_security_scope_paper"),
        UniqueConstraint("installation_id", name="uq_security_scope_installation"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    installation_id: Mapped[int | None] = mapped_column(ForeignKey("security_installations.id"))
    assignment_id: Mapped[UUID | None] = mapped_column(ForeignKey("assignments.id"))
    paper_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("paper_versions.id", name="fk_security_scope_paper")
    )

    exam_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("exam_events.id", name="fk_security_scope_event")
    )
    exam_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("exam_sessions.id", name="fk_security_scope_session")
    )


class SecurityRoleVersion(Base):
    __tablename__ = "security_role_versions"
    __table_args__ = (
        UniqueConstraint("role_code", "revision", name="uq_security_role_version"),
        CheckConstraint("revision > 0", name="ck_security_role_revision"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    role_code: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SecurityRolePermission(Base):
    __tablename__ = "security_role_permissions"
    role_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("security_role_versions.id"),
        primary_key=True,
    )
    permission_code: Mapped[str] = mapped_column(
        ForeignKey("permission_definitions.code"),
        primary_key=True,
    )


class SecurityGrant(Base):
    __tablename__ = "security_grants"
    __table_args__ = (
        Index("ix_security_grants_user_scope", "user_id", "scope_id"),
        Index(
            "uq_security_grants_active",
            "user_id",
            "role_version_id",
            "scope_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    role_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("security_role_versions.id"),
        nullable=False,
    )
    scope_id: Mapped[UUID] = mapped_column(ForeignKey("security_scopes.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SecurityAudit(Base):
    __tablename__ = "security_audit_events"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_security_audit_request"),
        CheckConstraint("length(payload_sha256) = 64", name="ck_security_audit_sha"),
        CheckConstraint(
            "action IN ('bootstrap','grant','revoke','protected_create')",
            name="ck_security_audit_action",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
