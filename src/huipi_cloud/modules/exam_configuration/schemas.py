"""Bounded web contracts; scope and actor always come from the authenticated server."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from huipi_cloud.modules.exam_rosters.schemas import EntriesRequest
from huipi_cloud.modules.exams.schemas import Schedule
from huipi_cloud.modules.organization.schemas import (
    AcademicTermSummary,
    AcademicYearSummary,
    AdministrativeClassSummary,
    SubjectSummary,
)
from huipi_cloud.modules.papers.schemas import Request


class ConfigurationEventView(BaseModel):
    id: UUID
    academic_term_id: UUID
    title: str
    category: str
    lifecycle: str
    revision: int
    term_label: str
    actions: list[str] = Field(default_factory=list)


class ConfigurationPaperChoice(BaseModel):
    paper_version_id: UUID
    title: str
    version_no: int
    status: str
    question_count: int
    max_points: str
    rubric_complete: bool
    content_current: bool


class ConfigurationBindingView(ConfigurationPaperChoice):
    id: UUID
    variant_code: str
    status: str
    paper_status: str


class ConfigurationSessionView(BaseModel):
    id: UUID
    exam_event_id: UUID
    subject_id: UUID
    title: str
    starts_at: datetime
    ends_at: datetime
    paper_policy: str
    lifecycle: str
    revision: int
    event_title: str
    subject_name: str
    term_label: str
    actions: list[str] = Field(default_factory=list)
    papers: list[ConfigurationBindingView] = Field(default_factory=list)


class ConfigurationEventPage(BaseModel):
    items: list[ConfigurationEventView]
    has_more: bool


class ConfigurationSessionPage(BaseModel):
    items: list[ConfigurationSessionView]
    has_more: bool


class ConfigurationPaperPage(BaseModel):
    items: list[ConfigurationPaperChoice]
    has_more: bool


class ConfigurationOptions(BaseModel):
    can_create_event: bool
    organization_available: bool
    years: list[AcademicYearSummary]
    terms: list[AcademicTermSummary]
    subjects: list[SubjectSummary]
    classes: list[AdministrativeClassSummary]
    truncated: bool


class ConfigurationSessionCreateBody(Schedule):
    subject_id: UUID
    expected_event_revision: int = Field(ge=1)


class ConfigurationRevisionRequest(Request):
    expected_revision: int = Field(ge=1)


class ConfigurationSessionStateBody(ConfigurationRevisionRequest):
    lifecycle: Literal["ready", "closed", "cancelled"]


class ConfigurationEventStateBody(ConfigurationRevisionRequest):
    lifecycle: Literal["active", "closed", "cancelled"]


class ConfigurationBindBody(ConfigurationRevisionRequest):
    paper_version_id: UUID
    variant_code: Literal["A", "B"]


class ConfigurationUnbindBody(ConfigurationRevisionRequest):
    session_paper_id: UUID


class ConfigurationCandidateRequest(Request):
    class_ids: list[UUID] = Field(min_length=1, max_length=20)
    expected_session_revision: int = Field(ge=1)
    offset: int = Field(default=0, ge=0, le=2000)
    limit: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def unique_classes(self):
        if len(set(self.class_ids)) != len(self.class_ids):
            raise ValueError("duplicate_class")
        return self


class ConfigurationCandidateStudent(BaseModel):
    student_id: UUID
    student_name: str
    student_code: str
    class_id: UUID
    class_name: str
    exam_number: str | None = None
    session_paper_id: UUID | None = None
    issues: list[str]


class ConfigurationCandidatePage(BaseModel):
    business_date: date
    session_revision: int
    students: list[ConfigurationCandidateStudent]
    total: int
    has_more: bool
    offset: int
    class_counts: dict[str, int]
    warnings: list[str]


class ConfigurationRosterCreateBody(EntriesRequest):
    business_date: date
    reason: str = Field(min_length=1, max_length=300)


class ConfigurationRosterReplaceBody(EntriesRequest):
    expected_revision: int = Field(ge=1)


class ConfigurationRosterCorrectionBody(ConfigurationRosterReplaceBody):
    reason: str = Field(min_length=1, max_length=300)


class ConfigurationRosterSummary(BaseModel):
    id: UUID
    exam_session_id: UUID
    version_no: int
    previous_revision_id: UUID | None
    business_date: date
    status: str
    is_current: bool
    revision: int
    reason: str
    frozen_at: datetime | None


class ConfigurationRosterEntry(BaseModel):
    id: UUID
    participation_id: UUID
    student_id: UUID
    student_name: str
    student_code: str
    administrative_class_id: UUID
    class_name: str
    exam_number: str | None
    session_paper_id: UUID | None


class ConfigurationRosterPage(BaseModel):
    roster: ConfigurationRosterSummary
    entries: list[ConfigurationRosterEntry]
    total: int
    offset: int
    limit: int
    truncated: bool
    unknown_variant_count: int
    attendance_status: Literal["not_recorded"]


class ConfigurationRosterHistory(BaseModel):
    revisions: list[ConfigurationRosterSummary]


class ConfigurationWriteResult(BaseModel):
    id: UUID
    revision: int
    related_id: UUID | None = None
