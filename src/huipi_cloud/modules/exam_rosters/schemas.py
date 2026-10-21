"""Explicit student identities; names, actors and roles are server-derived."""

from datetime import date
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from huipi_cloud.modules.papers.schemas import Request

MAX_ENTRIES = 2000


class EntrySelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    student_id: UUID
    exam_number: str | None = Field(default=None, pattern=r"^[A-Z0-9_-]{1,32}$")
    session_paper_id: UUID | None = None

    @field_validator("exam_number", mode="before")
    @classmethod
    def normalize_number(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class EntriesRequest(Request):
    entries: list[EntrySelection] = Field(max_length=MAX_ENTRIES)

    @model_validator(mode="after")
    def unique_entries(self):
        students = [e.student_id for e in self.entries]
        numbers = [e.exam_number for e in self.entries if e.exam_number is not None]
        if len(set(students)) != len(students) or len(set(numbers)) != len(numbers):
            raise ValueError("roster_duplicate_student_or_exam_number")
        return self


class RosterCreate(EntriesRequest):
    exam_session_id: UUID
    business_date: date
    reason: str = Field(min_length=1, max_length=300)


class RosterWrite(Request):
    roster_revision_id: UUID
    expected_revision: int = Field(ge=1)


class RosterReplace(EntriesRequest, RosterWrite):
    pass


class RosterCorrection(EntriesRequest, RosterWrite):
    reason: str = Field(min_length=1, max_length=300)
