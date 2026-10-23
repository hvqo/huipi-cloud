from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from huipi_cloud.modules.answer_scripts.schemas import SourcePage
from huipi_cloud.modules.canonical_documents.protocol import CanonicalPage


class ParseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_revision: int = Field(ge=1, le=100, strict=True)


class ParseReceipt(BaseModel):
    job_id: UUID


class ParseStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID | None
    status: Literal[
        "not_started", "pending", "running", "retry_wait", "failed", "succeeded", "invalidated"
    ]
    current: bool
    answer_script_version_id: UUID | None = None
    failure_code: str | None = None
    page_count: int | None = None
    block_count: int | None = None
    canonical_artifact_id: UUID | None = None
    parsed_artifact_id: UUID | None = None
    manifest_sha256: str | None = None
    attempt_count: int = 0
    roster_revision_id: UUID | None = None
    roster_entry_id: UUID | None = None
    participation_id: UUID | None = None
    session_paper_id: UUID | None = None
    paper_version_id: UUID | None = None


class ParsePage(BaseModel):
    answer_script_version_id: UUID
    canonical_artifact_id: UUID
    page: CanonicalPage
    source: SourcePage
