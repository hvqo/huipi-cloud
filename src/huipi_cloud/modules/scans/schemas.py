"""Minimal public HTTP contracts: never object keys, bucket names or student guesses."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID
    title: str = Field(min_length=1, max_length=120)


class BatchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    exam_session_id: UUID
    roster_revision_id: UUID | None
    title: str
    security_classification: Literal["exam_protected"]
    created_at: datetime


class FileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    batch_id: UUID
    request_id: UUID
    original_filename: str
    content_type: Literal["application/pdf"]
    source: Literal["manual_pdf"]
    size_bytes: int
    sha256: str
    status: Literal["receiving", "stored", "failed"]
    attempt_number: int
    lease_expires_at: datetime
    page_count: int | None
    failure_code: str | None
    created_at: datetime
    updated_at: datetime


class PageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    file_id: UUID
    page_index: int
    identity_status: Literal["unconfirmed"]
    width: int
    height: int
    size_bytes: int
    sha256: str


class ScanSession(BaseModel):
    id: UUID
    event_title: str
    title: str
    subject: str
    starts_at: datetime
    lifecycle: str
    roster_frozen: bool


class ScanLimits(BaseModel):
    max_file_bytes: int
    max_pages: int
    max_files_per_batch: int = 20
    max_preview_edge: int = 1600


class SessionPage(BaseModel):
    items: list[ScanSession]
    offset: int
    limit: int
    truncated: bool
    limits: ScanLimits


class BatchPage(BaseModel):
    items: list[BatchRead]
    truncated: bool


class FilePage(BaseModel):
    items: list[FileRead]
    truncated: bool


class PagePage(BaseModel):
    items: list[PageRead]
    truncated: bool
