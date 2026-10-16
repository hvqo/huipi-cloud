"""Stable frontend contracts. Roles and capabilities are database projections."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class Role(StrEnum):
    TEACHER = "teacher"
    ADMIN = "admin"


class Capability(StrEnum):
    CREATE_ASSIGNMENT = "assignment:create"
    READ_SCOPED = "assignment:read_scoped"
    WRITE_SCOPED = "assignment:write_scoped"
    REVIEW_SCOPED = "review:write_scoped"
    ACCESS_ALL = "assignment:access_all"
    MANAGE_TEACHERS = "teacher:manage"
    MANAGE_MEMBERSHIPS = "membership:manage"
    READ_AUDIT = "audit:read"


class CurrentUser(BaseModel):
    user_id: UUID
    display_name: str | None = None
    role: Role
    capabilities: list[Capability]
    is_active: bool


class AdminUserSummary(BaseModel):
    user_id: UUID
    role: Role = Role.TEACHER
    is_active: bool
    created_at: datetime


class TeacherPage(BaseModel):
    records: list[AdminUserSummary]
    limit: int
    offset: int
    has_next: bool
    next_offset: int | None


class TeacherRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    issuer: str = Field(min_length=1, max_length=512)
    subject: str = Field(min_length=1, max_length=255)

    @field_validator("issuer", "subject")
    @classmethod
    def exact_identity(cls, value: str) -> str:
        # OIDC identities are exact, case-sensitive values; never silently normalize.
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("identity must not contain surrounding whitespace or controls")
        return value


class TeacherActivation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    is_active: StrictBool


class AssignmentMembershipSummary(BaseModel):
    assignment_id: UUID
    user_id: UUID
    can_write: bool
    is_active: bool
    created_at: datetime
    revoked_at: datetime | None


class MembershipPage(BaseModel):
    records: list[AssignmentMembershipSummary]
    limit: int
    offset: int
    has_next: bool
    next_offset: int | None


class AdminAction(StrEnum):
    REGISTER_TEACHER = "teacher_registered"
    ACTIVATE_TEACHER = "teacher_activation_changed"
    GRANT_ASSIGNMENT = "assignment_membership_granted"
    REVOKE_ASSIGNMENT = "assignment_membership_revoked"


class AdminOperationResult(BaseModel):
    operation_id: UUID
    request_id: UUID
    action: AdminAction
    target_user_id: UUID
    assignment_id: UUID | None
    state: dict[str, bool]
    created_at: datetime


class AdminAuditSummary(AdminOperationResult):
    actor_user_id: UUID


class AdminAuditPage(BaseModel):
    records: list[AdminAuditSummary]
    limit: int
    offset: int
    has_next: bool
    next_offset: int | None
