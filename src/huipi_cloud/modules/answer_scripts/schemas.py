"""Client selects trusted entries, never supplies a student identity or storage locator."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ScriptCreate(Strict):
    request_id: UUID
    page_ids: list[UUID] = Field(min_length=1, max_length=100)
    completeness: Literal["complete", "incomplete"] = "incomplete"
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def unique_pages(self):
        if len(set(self.page_ids)) != len(self.page_ids):
            raise ValueError("duplicate_scan_page")
        return self


class ScriptChange(ScriptCreate):
    expected_revision: int = Field(ge=1, le=100)
    action: Literal["save_draft", "confirm", "revoke"]
    roster_revision_id: UUID | None = None
    roster_entry_id: UUID | None = None
    session_paper_id: UUID | None = None

    @model_validator(mode="after")
    def identity_shape(self):
        ids = (self.roster_revision_id, self.roster_entry_id, self.session_paper_id)
        if (self.action == "confirm" and not all(ids)) or (self.action != "confirm" and any(ids)):
            raise ValueError("explicit_current_identity_required")
        return self


class ScriptReceipt(Strict):
    script_id: UUID
    version_id: UUID
    revision: int
    status: Literal["draft", "confirmed", "revoked"]


class SourcePage(Strict):
    scan_page_id: UUID
    file_id: UUID
    original_filename: str
    batch_id: UUID
    source_page_index: int
    page_sha256: str
    file_sha256: str
    allocated_script_id: UUID | None = None


class SourcePageList(Strict):
    items: list[SourcePage]
    total: int
    offset: int
    limit: int


class IdentityCandidate(Strict):
    roster_entry_id: UUID
    participation_id: UUID
    student_name: str
    student_code: str
    class_name: str
    exam_number: str | None
    session_paper_id: UUID | None


class PaperChoice(Strict):
    session_paper_id: UUID
    paper_version_id: UUID
    variant_code: str


class CandidateList(Strict):
    exam_session_id: UUID
    roster_revision_id: UUID | None
    roster_version_no: int | None
    items: list[IdentityCandidate]
    papers: list[PaperChoice]
    total: int
    offset: int
    limit: int


class VersionRead(Strict):
    id: UUID
    version_no: int
    previous_version_id: UUID | None
    status: Literal["draft", "confirmed", "revoked"]
    completeness: Literal["complete", "incomplete"]
    reason: str
    manifest_sha256: str
    created_at: datetime
    roster_revision_id: UUID | None
    roster_entry_id: UUID | None
    participation_id: UUID | None
    session_paper_id: UUID | None
    identity: IdentityCandidate | None
    pages: list[SourcePage]


class ScriptRead(Strict):
    script_id: UUID
    exam_session_id: UUID
    revision: int
    current: VersionRead
    readiness: str
    identity_visible: bool


class ScriptList(Strict):
    items: list[ScriptRead]
    total: int
    offset: int
    limit: int


class VersionList(Strict):
    items: list[VersionRead]
    total: int
    offset: int
    limit: int


class ScriptManifest(Strict):
    manifest_version: Literal["1.0"] = "1.0"
    script_id: UUID
    answer_script_version_id: UUID
    exam_session_id: UUID
    roster_revision_id: UUID
    roster_entry_id: UUID
    participation_id: UUID
    session_paper_id: UUID
    paper_version_id: UUID
    manifest_sha256: str
    pages: list[SourcePage]
    # Not a durable permission or a ParsingTask. A future bridge MUST revalidate at commit.
    validated_at: datetime
    bridge_implemented: Literal[True] = True
