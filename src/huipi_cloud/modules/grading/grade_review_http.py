"""Authenticated HTTP DTOs; immutable CLI and artifact protocol 1.0 stays unchanged."""

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from huipi_cloud.modules.grading.grade_review_protocol import (
    GradeReviewCriterionInput,
    GradeReviewDecisionCreate,
    GradeReviewHistoryCriterion,
    GradeReviewInspection,
    ReviewReasonCode,
    ReviewStatus,
)


class GradeReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    expected_revision: int = Field(ge=0, strict=True)
    review_status: ReviewStatus
    review_reason_codes: list[ReviewReasonCode] = Field(default_factory=list, max_length=10)
    review_comment: str | None = Field(default=None, max_length=2000)
    criterion_reviews: list[GradeReviewCriterionInput] = Field(default_factory=list, max_length=100)

    def for_reviewer(self, user_id: UUID) -> GradeReviewDecisionCreate:
        return GradeReviewDecisionCreate(
            schema_name="huipi.grading.review",
            schema_version="1.0",
            reviewer_ref=f"teacher_{user_id.hex}",
            **self.model_dump(exclude={"request_id", "expected_revision"}),
        )

    @model_validator(mode="after")
    def validate_original_rules(self):
        self.for_reviewer(UUID(int=0))
        return self


class GradeRevisionSummary(BaseModel):
    review_id: UUID
    proposal_id: UUID
    grade_input_artifact_id: UUID
    proposal_sha256: str
    grade_input_sha256: str
    review_revision: int
    supersedes_review_id: UUID | None
    reviewer_ref: str
    reviewer_user_id: UUID | None
    reviewer_kind: Literal["authenticated_teacher", "legacy_cli_claim"]
    review_reason_codes: list[ReviewReasonCode] = Field(max_length=10)
    review_comment_present: bool
    request_id: UUID
    review_status: ReviewStatus
    proposed_total_score: Decimal | None
    reviewed_total_score: Decimal | None
    criterion_scores: list[GradeReviewHistoryCriterion] = Field(max_length=100)
    created_at: datetime
    grade_published: Literal[False] = False


class GradeEvidenceLocation(BaseModel):
    criterion_id: UUID
    segment_index: int
    content_pointer: str
    page_number: int
    text_start: int
    text_end: int
    canonical_text_start: int
    canonical_text_end: int
    canonical_page_path: str


class GradeReviewHTTPInspection(GradeReviewInspection):
    reviewer_identity_authenticated: bool = False
    latest: GradeRevisionSummary | None
    regrading_required: bool
    has_review_history: bool
    evidence_locations: list[GradeEvidenceLocation] = Field(default_factory=list, max_length=25)
    evidence_offset: int = 0
    evidence_truncated: bool = False
    next_evidence_offset: int | None = None


class GradeReviewHTTPHistory(BaseModel):
    proposal_id: UUID
    records: list[GradeRevisionSummary] = Field(max_length=10)
    limit: int
    offset: int
    has_next: bool
    next_offset: int | None
    source_validity: Literal["not_checked"] = "not_checked"
    grade_published: Literal[False] = False


class GradeReviewHTTPWriteResult(BaseModel):
    created: bool
    record: GradeRevisionSummary
    grade_published: Literal[False] = False
