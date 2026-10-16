"""Local identity mapping and revocable assignment membership; no passwords."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
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
