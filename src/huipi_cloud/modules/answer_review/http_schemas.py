"""Strict HTTP DTOs, separate from the immutable 1.0 CLI/persistence protocol."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from huipi_cloud.modules.answer_review.protocol import (
    AnswerPresenceDecision,
    AnswerReviewDecisionCreate,
    ReviewReasonCode,
    ReviewRegionSelection,
    ReviewSourceVersion,
)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    expected_revision: int = Field(ge=0, strict=True)
    expected_source: ReviewSourceVersion
    decision: AnswerPresenceDecision
    response_regions: list[ReviewRegionSelection] = Field(default_factory=list, max_length=100)
    excluded_prompt_regions: list[ReviewRegionSelection] = Field(
        default_factory=list, max_length=100
    )
    uncertain_regions: list[ReviewRegionSelection] = Field(default_factory=list, max_length=100)
    reason_codes: list[ReviewReasonCode] = Field(min_length=1, max_length=20)

    def for_reviewer(self, user_id):
        return AnswerReviewDecisionCreate(
            **self.model_dump(exclude={"request_id", "expected_revision", "expected_source"}),
            reviewer_ref=f"teacher_{user_id.hex}",
        )

    @model_validator(mode="after")
    def validate_legacy_evidence_rules(self):
        # Reuse the exact original role/region validator; dummy identity never persists.
        self.for_reviewer(UUID(int=0))
        if self.decision == "response_present" and {
            "printed_prompt_only",
            "blank_response_area",
            "shared_figure_attribution_unclear",
        }.intersection(self.reason_codes):
            raise ValueError("confirmed response contradicts stated prompt/blank/shared evidence")
        if self.decision == "response_absent" and "student_work_visible" in self.reason_codes:
            raise ValueError("absence contradicts stated visible student work")
        return self


class RevisionSummary(BaseModel):
    decision_id: UUID
    question_id: UUID
    decision: AnswerPresenceDecision
    revision: int
    request_id: UUID
    supersedes_decision_id: UUID | None
    created_at: datetime
    reviewer_ref: str
    reviewer_kind: Literal["authenticated_teacher", "legacy_cli_claim"]
    reviewer_user_id: UUID | None
    source: ReviewSourceVersion
    region_counts: dict[str, int]
    reason_codes: list[str]


class ReviewHistory(BaseModel):
    submission_id: UUID
    records: list[RevisionSummary]
    offset: int
    limit: int
    has_next: bool
    next_offset: int | None
    source_validity: Literal["not_checked"] = "not_checked"


class EvidenceSummary(BaseModel):
    content_pointer: str
    page_number: int
    source_block_id: UUID
    text_start: int | None
    text_end: int | None
    normalized_type: str
    asset_count: int
    canonical_page_path: str


class ReviewInspection(BaseModel):
    submission_id: UUID
    question_id: UUID
    source: ReviewSourceVersion
    alignment_status: str
    answer_presence: Literal["unreviewed", "response_present", "response_absent", "uncertain"]
    expected_revision: int
    latest: RevisionSummary | None
    latest_source_matches: bool
    evidence: list[EvidenceSummary]
    evidence_offset: int
    evidence_truncated: bool
    next_evidence_offset: int | None
    meaning: Literal["human_answer_presence_not_grading"] = "human_answer_presence_not_grading"


class ReviewWriteResult(BaseModel):
    created: bool
    record: RevisionSummary
    source_validity: Literal["validated_at_commit"] = "validated_at_commit"
