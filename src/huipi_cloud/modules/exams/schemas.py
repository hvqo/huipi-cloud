"""Strict controlled-CLI contracts, with aware times and no client authority claims."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from huipi_cloud.modules.papers.schemas import Request


class EventCreate(Request):
    academic_term_id: UUID
    title: str = Field(min_length=1, max_length=200)
    category: Literal["midterm", "final", "monthly", "other"] = "other"


class EventWrite(Request):
    exam_event_id: UUID
    expected_revision: int = Field(ge=1)


class EventEdit(EventWrite):
    title: str = Field(min_length=1, max_length=200)
    category: Literal["midterm", "final", "monthly", "other"]


class EventState(EventWrite):
    lifecycle: Literal["active", "closed", "cancelled"]


class Schedule(Request):
    title: str = Field(min_length=1, max_length=200)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    paper_policy: Literal["single", "ab"] = "single"

    @model_validator(mode="after")
    def ordered(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("exam_time_order")
        return self


class SessionCreate(Schedule):
    exam_event_id: UUID
    subject_id: UUID
    expected_event_revision: int = Field(ge=1)


class SessionWrite(Request):
    exam_session_id: UUID
    expected_revision: int = Field(ge=1)


class SessionEdit(Schedule):
    exam_session_id: UUID
    expected_revision: int = Field(ge=1)


class SessionState(SessionWrite):
    lifecycle: Literal["ready", "closed", "cancelled"]


class PaperBind(SessionWrite):
    paper_version_id: UUID
    variant_code: Literal["A", "B"] = "A"


class PaperUnbind(SessionWrite):
    session_paper_id: UUID
