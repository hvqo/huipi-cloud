"""Internal users, locally managed credentials, sessions and permissions."""

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
    false,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),
        CheckConstraint("length(trim(issuer)) > 0", name="ck_users_issuer_not_blank"),
        CheckConstraint("length(trim(subject)) > 0", name="ck_users_subject_not_blank"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LocalCredential(Base):
    """A local login attached to one existing internal user; never store plaintext."""

    __tablename__ = "local_credentials"
    __table_args__ = (
        UniqueConstraint("login_name", name="uq_local_credentials_login_name"),
        UniqueConstraint("recovery_email", name="uq_local_credentials_recovery_email"),
        CheckConstraint("length(login_name) BETWEEN 3 AND 254", name="ck_local_login_length"),
        CheckConstraint(
            "(recovery_email IS NULL AND recovery_verified_at IS NULL) OR "
            "(recovery_email IS NOT NULL AND recovery_verified_at IS NOT NULL)",
            name="ck_local_recovery_email_verified",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
    )
    login_name: Mapped[str] = mapped_column(String(254), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100))
    recovery_email: Mapped[str | None] = mapped_column(String(254))
    recovery_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LocalSession(Base):
    __tablename__ = "local_sessions"
    __table_args__ = (
        CheckConstraint("length(token_hash) = 64", name="ck_local_session_token_hash"),
        CheckConstraint("length(csrf_hash) = 64", name="ck_local_session_csrf_hash"),
        Index("ix_local_sessions_user_active", "user_id", "expires_at"),
    )
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LocalAuthThrottle(Base):
    __tablename__ = "local_auth_throttles"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LocalPasswordReset(Base):
    __tablename__ = "local_password_resets"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_local_password_reset_token"),
        CheckConstraint("length(token_hash) = 64", name="ck_local_password_reset_token_hash"),
        CheckConstraint(
            "purpose IN ('verify_email', 'reset_password') AND target_email IS NOT NULL",
            name="ck_local_password_reset_purpose",
        ),
        Index("ix_local_password_resets_user", "user_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    target_email: Mapped[str | None] = mapped_column(String(254))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LocalAuthAudit(Base):
    __tablename__ = "local_auth_audit"
    __table_args__ = (
        Index("ix_local_auth_audit_target_created", "target_user_id", "created_at"),
        UniqueConstraint("actor_user_id", "request_id", name="uq_local_auth_audit_actor_request"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID | None] = mapped_column(Uuid)
    target_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    request_id: Mapped[UUID | None] = mapped_column(Uuid)
    payload_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssignmentMembership(Base):
    __tablename__ = "assignment_memberships"
    __table_args__ = (
        UniqueConstraint("assignment_id", "user_id", name="uq_assignment_memberships_user"),
        Index("ix_assignment_memberships_user_assignment", "user_id", "assignment_id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    assignment_id: Mapped[UUID] = mapped_column(
        ForeignKey("assignments.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    can_write: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminOperation(Base):
    """Append-only administrative facts; UUID references survive account/resource removal."""

    __tablename__ = "admin_operations"
    __table_args__ = (
        UniqueConstraint("actor_user_id", "request_id", name="uq_admin_operations_request"),
        CheckConstraint("length(payload_sha256) = 64", name="ck_admin_operations_digest"),
        CheckConstraint("jsonb_typeof(state) = 'object'", name="ck_admin_operations_state"),
        CheckConstraint(
            "action IN ('teacher_registered', 'teacher_activation_changed', "
            "'assignment_membership_granted', 'assignment_membership_revoked')",
            name="ck_admin_operations_action",
        ),
        Index("ix_admin_operations_target_created", "target_user_id", "created_at"),
        Index("ix_admin_operations_assignment_created", "assignment_id", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    target_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    assignment_id: Mapped[UUID | None] = mapped_column(Uuid)
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
