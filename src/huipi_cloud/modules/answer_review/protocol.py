"""Versioned protocol for human answer-presence decisions and source evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

ANSWER_REVIEW_SCHEMA_VERSION = "1.0"

AnswerPresenceDecision = Literal["response_present", "response_absent", "uncertain"]
AlignmentStatus = Literal["aligned", "review_required", "not_observed"]
ReviewReasonCode = Literal[
    "student_work_visible",
    "printed_prompt_only",
    "blank_response_area",
    "ambiguous_handwriting",
    "ocr_incomplete",
    "formula_only_response",
    "table_entry_visible",
    "diagram_marks_visible",
    "shared_figure_attribution_unclear",
    "response_not_linked_to_question",
    "source_quality_insufficient",
    "multiple_response_regions",
    "cross_page_response",
]


class ReviewRegionSelection(BaseModel):
    """A reviewer selection on one existing Canonical ContentNode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content_pointer: str = Field(min_length=1, max_length=4096)
    text_start: int | None = Field(default=None, ge=0, strict=True)
    text_end: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_offsets(self) -> ReviewRegionSelection:
        if (self.text_start is None) != (self.text_end is None):
            raise ValueError("text_start and text_end must be supplied together")
        if self.text_start is not None and self.text_end is not None:
            if self.text_end <= self.text_start:
                raise ValueError("text offsets must define a non-empty half-open range")
        return self


class ReviewAssetReference(BaseModel):
    """Safe logical asset metadata; private or external URIs are never returned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["stored", "external", "inline_redacted"]
    asset_id: UUID | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    content_type: str | None = Field(default=None, max_length=100)
    source_pointer: str | None = Field(default=None, max_length=4096)
    source_encoding: Literal["base64", "data_uri", "html_data_uri"] | None = None
    source_occurrence_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_safe_shape(self) -> ReviewAssetReference:
        if self.kind == "stored" and self.asset_id is None:
            raise ValueError("stored asset references require an asset_id")
        if self.kind == "inline_redacted" and not all(
            (
                self.sha256,
                self.size_bytes is not None,
                self.content_type,
                self.source_pointer,
                self.source_encoding,
            )
        ):
            raise ValueError("inline image references require redacted source metadata")
        return self


class ReviewEvidenceRegion(BaseModel):
    """Validated trace metadata copied from the immutable Canonical source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content_pointer: str = Field(min_length=1, max_length=4096)
    page_index: int = Field(ge=0)
    page_number: int = Field(ge=1)
    source_block_id: UUID
    reading_order: int = Field(ge=0)
    bbox: tuple[float, float, float, float] | None = None
    source_type: str = Field(min_length=1, max_length=64)
    normalized_type: str = Field(min_length=1, max_length=32)
    text_start: int | None = Field(default=None, ge=0)
    text_end: int | None = Field(default=None, ge=0)
    asset_refs: list[ReviewAssetReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_offsets(self) -> ReviewEvidenceRegion:
        if (self.text_start is None) != (self.text_end is None):
            raise ValueError("text_start and text_end must be supplied together")
        if self.text_start is not None and self.text_end is not None:
            if self.text_end <= self.text_start:
                raise ValueError("text offsets must define a non-empty half-open range")
        return self


class AnswerReviewDecisionCreate(BaseModel):
    """One proposed decision. Identity and content hashes come from the server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: AnswerPresenceDecision
    response_regions: list[ReviewRegionSelection] = Field(default_factory=list, max_length=100)
    excluded_prompt_regions: list[ReviewRegionSelection] = Field(
        default_factory=list,
        max_length=100,
    )
    uncertain_regions: list[ReviewRegionSelection] = Field(default_factory=list, max_length=100)
    reason_codes: list[ReviewReasonCode] = Field(min_length=1, max_length=20)
    reviewer_ref: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")

    @model_validator(mode="after")
    def validate_decision_regions(self) -> AnswerReviewDecisionCreate:
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("reason_codes must not contain duplicates")
        roles = (
            self.response_regions,
            self.excluded_prompt_regions,
            self.uncertain_regions,
        )
        seen: dict[str, list[ReviewRegionSelection]] = {}
        for regions in roles:
            for region in regions:
                for prior in seen.setdefault(region.content_pointer, []):
                    if _selections_overlap(prior, region):
                        raise ValueError("selected source regions must not overlap or repeat")
                seen[region.content_pointer].append(region)

        if self.decision == "response_present":
            if not self.response_regions:
                raise ValueError("response_present requires at least one response region")
            if self.uncertain_regions:
                raise ValueError("response_present cannot include uncertain regions")
        elif self.decision == "response_absent":
            if self.response_regions or self.uncertain_regions:
                raise ValueError("response_absent cannot include response or uncertain regions")
            if not self.excluded_prompt_regions:
                raise ValueError("response_absent requires a cited prompt or response-area region")
        else:
            if self.response_regions:
                raise ValueError("uncertain decisions must use uncertain_regions")
            if not self.uncertain_regions and not {
                "source_quality_insufficient",
                "response_not_linked_to_question",
            }.intersection(self.reason_codes):
                raise ValueError(
                    "uncertain decisions require an uncertain source or explicit reason"
                )
        return self


class AnswerReviewDecisionRead(BaseModel):
    """Persisted immutable revision tied to one precise source version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: UUID
    submission_id: UUID
    assignment_id: UUID
    question_id: UUID
    question_number: int = Field(gt=0)
    alignment_status: AlignmentStatus
    alignment_artifact_id: UUID
    alignment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aligner_version: str = Field(min_length=1, max_length=32)
    assignment_questions_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_document_id: UUID
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_schema_version: Literal["1.0"] = ANSWER_REVIEW_SCHEMA_VERSION
    decision: AnswerPresenceDecision
    response_regions: list[ReviewEvidenceRegion]
    excluded_prompt_regions: list[ReviewEvidenceRegion]
    uncertain_regions: list[ReviewEvidenceRegion]
    reason_codes: list[ReviewReasonCode]
    reviewer_ref: str
    request_id: UUID
    revision: int = Field(ge=1)
    supersedes_decision_id: UUID | None = None
    created_at: datetime


def _selections_overlap(
    first: ReviewRegionSelection,
    second: ReviewRegionSelection,
) -> bool:
    if first.text_start is None or second.text_start is None:
        return True
    assert first.text_end is not None and second.text_end is not None
    return max(first.text_start, second.text_start) < min(first.text_end, second.text_end)
