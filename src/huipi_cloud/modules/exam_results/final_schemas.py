"""Explicit attendance, version creation, and publication operations."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AttendanceInput(Input):
    request_id: UUID
    participation_id: UUID
    expected_revision: int = Field(ge=0, le=1000)
    status: Literal["present", "absent", "unverified"]
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value):
        if not value.strip():
            raise ValueError("reason is required")
        return value.strip()


class PrepareInput(Input):
    request_id: UUID
    previous_version_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value):
        if not value.strip():
            raise ValueError("reason is required")
        return value.strip()


class TransitionInput(Input):
    request_id: UUID
    expected_revision: int = Field(ge=1)
    action: Literal["approve", "publish", "cancel"]
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value):
        if not value.strip():
            raise ValueError("reason is required")
        return value.strip()
