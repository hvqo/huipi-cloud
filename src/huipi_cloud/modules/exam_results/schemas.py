"""Score-only projections. No answers, quotes, object keys or published grades."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClassSummary(ReadModel):
    class_id: UUID
    class_name: str


class SessionSummary(ReadModel):
    session_id: UUID
    event_id: UUID
    event_title: str
    title: str
    classes: list[ClassSummary]


class SessionsRead(ReadModel):
    items: list[SessionSummary]
    truncated: bool


class QuestionResult(ReadModel):
    mapping_id: UUID
    question_id: UUID
    number: int
    max_points: str
    status: Literal[
        "reviewed",
        "not_scored",
        "awaiting_review",
        "regrading_required",
        "rejected",
        "abstained",
        "source_stale",
    ]
    points: str | None = None
    workspace_id: UUID | None = None
    proposal_id: UUID | None = None
    review_id: UUID | None = None
    review_revision: int | None = None
    review_status: str | None = None
    reason: str | None = None
    evidence_source: Literal["teacher_review", "visual", "omr"] | None = None
    omr_decision_id: UUID | None = None


class StudentResult(ReadModel):
    participation_id: UUID
    roster_entry_id: UUID
    student_id: UUID
    student_name: str
    student_code: str
    exam_number: str | None
    class_id: UUID
    class_name: str
    session_paper_id: UUID | None = None
    paper_version_id: UUID | None = None
    variant: str | None = None
    script_id: UUID | None = None
    script_version_id: UUID | None = None
    reviewed_count: int = 0
    required_count: int | None = None
    subtotal: str | None = None
    total: str | None = None
    max_points: str | None = None
    status: Literal[
        "complete", "incomplete", "identity_pending", "source_stale", "variant_unverified"
    ]
    attendance_status: Literal["not_determined"] = "not_determined"
    reasons: list[str] = Field(default_factory=list)
    questions: list[QuestionResult] = Field(default_factory=list)


class Preview(ReadModel):
    session_id: UUID
    event_id: UUID
    event_title: str
    session_title: str
    roster_revision_id: UUID | None
    generated_at: datetime
    snapshot_sha256: str
    published: Literal[False] = False
    classes: list[ClassSummary]
    students: list[StudentResult]
    offset: int
    limit: int
    total_students: int
    has_more: bool
    scope_note: Literal["explicit_classes_only"] = "explicit_classes_only"
