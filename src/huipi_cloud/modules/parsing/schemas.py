"""Safe parsing task API schemas."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ParsingTaskStatusRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: UUID = Field(validation_alias="id")
    submission_id: UUID
    status: Literal["pending", "running", "retry_wait", "succeeded", "failed"]
    attempt_count: int
    max_attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    next_run_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None


class ParsedArtifactSummary(BaseModel):
    """Public parser provenance and checksums, without private object keys."""

    model_config = ConfigDict(from_attributes=True)

    artifact_id: UUID = Field(validation_alias="id")
    parser_name: str
    parser_version: str
    tier: str
    schema_name: str
    schema_version: str
    page_count: int
    asset_count: int
    original_sha256: str
    archive_sha256: str
    markdown_sha256: str
    middle_json_sha256: str
    structured_content_sha256: str
    created_at: datetime


class ParsedDocumentRead(BaseModel):
    """Current task status and a result summary when one is indexed."""

    submission_id: UUID
    status: Literal["pending", "running", "retry_wait", "succeeded", "failed"]
    parsed_document: ParsedArtifactSummary | None
