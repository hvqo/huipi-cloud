from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

Kind = Literal[
    "upload_failed",
    "upload_interrupted",
    "unclaimed_page",
    "duplicate_page_check",
    "identity_pending",
    "incomplete_script",
    "source_changed",
    "parsing_failed",
    "parsing_invalidated",
    "parsing_not_started",
    "answer_unconfirmed",
    "answer_absent",
    "scoring_failed",
    "scoring_abstained",
    "scoring_invalidated",
    "task_invalidated",
]


class RecoveryItem(BaseModel):
    kind: Kind
    resource_id: UUID
    title: str
    reason: str
    state: Literal["pending", "manual_review"] = "pending"
    batch_id: UUID | None = None
    file_id: UUID | None = None
    page_id: UUID | None = None
    script_id: UUID | None = None
    workspace_id: UUID | None = None
    task_id: UUID | None = None
    retry_request_id: UUID | None = None
    original_filename: str | None = None
    updated_at: datetime


class RecoveryView(BaseModel):
    session_id: UUID
    session_title: str
    event_title: str
    items: list[RecoveryItem]
    counts: dict[str, int]
    total: int
    offset: int
    limit: int
    has_next: bool
    capabilities: dict[str, bool]
    results: dict[str, int]
    generated_at: datetime
