"""Versioned, append-only human review protocol for grading proposals."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from huipi_cloud.modules.grading.proposal_protocol import EvidenceReference
from huipi_cloud.modules.grading.protocol import canonical_json_bytes

GRADE_REVIEW_SCHEMA_NAME = "huipi.grading.review"
GRADE_REVIEW_SCHEMA_VERSION = "1.0"

ReviewStatus = Literal["accepted", "revised", "rejected", "regrading_required"]
ReviewReasonCode = Literal[
    "rubric_interpretation_error",
    "incorrect_partial_credit",
    "unsupported_score",
    "evidence_not_sufficient",
    "candidate_not_reliable",
    "source_quality_problem",
    "additional_evidence_required",
    "manual_follow_up_needed",
    "other",
]

_ReviewPoints = Annotated[
    Decimal,
    Field(ge=0, max_digits=8, decimal_places=2, allow_inf_nan=False),
]


class GradeReviewCriterionInput(BaseModel):
    """One human-entered score for a Criterion in a revised decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: UUID
    reviewed_points: _ReviewPoints
    rationale: str | None = Field(default=None, min_length=1, max_length=2000)
    evidence_references: list[EvidenceReference] | None = Field(default=None, max_length=50)

    @field_validator("reviewed_points", mode="before")
    @classmethod
    def reject_binary_floats(cls, value: object) -> object:
        if isinstance(value, float) or isinstance(value, bool):
            raise ValueError("reviewed_points must use an exact decimal value")
        return value

    @field_validator("rationale")
    @classmethod
    def reject_blank_rationale(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("rationale must not be blank")
        return value


class GradeReviewDecisionCreate(BaseModel):
    """Local JSON input. The service supplies all proposal and source identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["huipi.grading.review"]
    schema_version: Literal["1.0"]
    review_status: ReviewStatus
    reviewer_ref: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    review_reason_codes: list[ReviewReasonCode] = Field(default_factory=list, max_length=10)
    review_comment: str | None = Field(default=None, max_length=2000)
    criterion_reviews: list[GradeReviewCriterionInput] = Field(default_factory=list, max_length=100)

    @field_validator("review_comment")
    @classmethod
    def reject_blank_comment(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("review_comment must not be blank")
        return value

    @model_validator(mode="after")
    def validate_status_shape(self) -> Self:
        if len(self.review_reason_codes) != len(set(self.review_reason_codes)):
            raise ValueError("review_reason_codes must not contain duplicates")
        if self.review_status == "revised":
            if not self.criterion_reviews:
                raise ValueError("revised requires criterion_reviews")
        elif self.criterion_reviews:
            raise ValueError("only revised accepts criterion_reviews")
        if self.review_status in {"rejected", "regrading_required"}:
            if not self.review_reason_codes:
                raise ValueError("rejected and regrading_required need a reason code")
        if "other" in self.review_reason_codes and not self.review_comment:
            raise ValueError("other requires review_comment")
        return self


class GradeReviewCriterionReview(BaseModel):
    """Immutable comparison between the model proposal and reviewed score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: UUID
    proposed_points: _ReviewPoints
    reviewed_points: _ReviewPoints
    max_points: Decimal = Field(gt=0, max_digits=8, decimal_places=2, allow_inf_nan=False)
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_references: list[EvidenceReference] = Field(min_length=1, max_length=50)
    changed: bool

    @model_validator(mode="after")
    def validate_derived_change(self) -> Self:
        if self.reviewed_points > self.max_points:
            raise ValueError("reviewed_points exceeds max_points")
        if self.changed != (self.proposed_points != self.reviewed_points):
            raise ValueError("changed must reflect the score difference")
        return self


class GradeReviewRevisionDocument(BaseModel):
    """Persisted review revision bound to one immutable proposal and GradeInput."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["huipi.grading.review"] = GRADE_REVIEW_SCHEMA_NAME
    schema_version: Literal["1.0"] = GRADE_REVIEW_SCHEMA_VERSION
    review_id: UUID
    proposal_id: UUID
    grade_input_artifact_id: UUID
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    grade_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    submission_id: UUID
    question_id: UUID
    review_revision: int = Field(gt=0)
    supersedes_review_id: UUID | None = None
    reviewer_ref: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    request_id: UUID
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_status: ReviewStatus
    criterion_reviews: list[GradeReviewCriterionReview] = Field(max_length=100)
    review_reason_codes: list[ReviewReasonCode] = Field(max_length=10)
    review_comment: str | None = Field(default=None, max_length=2000)
    proposed_total_score: Decimal | None = Field(default=None, ge=0, max_digits=8, decimal_places=2)
    reviewed_total_score: Decimal | None = Field(default=None, ge=0, max_digits=8, decimal_places=2)
    created_at: datetime

    @model_validator(mode="after")
    def validate_review_result(self) -> Self:
        if self.review_status in {"accepted", "revised"}:
            if not self.criterion_reviews:
                raise ValueError("scored review requires Criterion reviews")
            if self.proposed_total_score is None or self.reviewed_total_score is None:
                raise ValueError("scored review requires both totals")
            if len({item.criterion_id for item in self.criterion_reviews}) != len(
                self.criterion_reviews
            ):
                raise ValueError("Criterion reviews must be unique")
            calculated = sum(
                (item.reviewed_points for item in self.criterion_reviews), Decimal("0.00")
            )
            proposed = sum(
                (item.proposed_points for item in self.criterion_reviews), Decimal("0.00")
            )
            if calculated != self.reviewed_total_score or proposed != self.proposed_total_score:
                raise ValueError("review totals must equal Criterion scores")
            if self.review_status == "accepted" and any(
                item.changed for item in self.criterion_reviews
            ):
                raise ValueError("accepted review cannot change scores")
            if self.review_status == "revised" and not any(
                item.changed for item in self.criterion_reviews
            ):
                raise ValueError("revised review must change at least one score")
        elif self.criterion_reviews or self.reviewed_total_score is not None:
            raise ValueError("unscored review cannot contain reviewed scores")
        return self


class GradeReviewRecordResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review: GradeReviewRevisionDocument
    created: bool


class GradeReviewCriterionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: UUID
    proposed_points: Decimal
    max_points: Decimal


class GradeReviewInspection(BaseModel):
    """Safe CLI inspection without answer text, rationales, or storage locators."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposal_id: UUID
    grading_status: Literal["completed_candidate", "grading_abstained"]
    source_status: Literal["current", "source_stale", "unavailable"]
    grade_input_artifact_id: UUID
    grade_input_sha256: str
    proposal_sha256: str | None = None
    submission_id: UUID
    question_id: UUID
    proposed_total_score: Decimal | None = None
    max_score: Decimal | None = None
    criterion_scores: list[GradeReviewCriterionSummary] = Field(default_factory=list)
    latest_review_revision: int = Field(default=0, ge=0)
    latest_review_status: ReviewStatus | None = None
    reviewer_identity_authenticated: Literal[False] = False
    grade_published: Literal[False] = False


class GradeReviewHistoryCriterion(BaseModel):
    """Score-only history row without rationale or answer quotes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: UUID
    proposed_points: Decimal
    reviewed_points: Decimal
    max_points: Decimal
    changed: bool


class GradeReviewHistoryItem(BaseModel):
    """Safe CLI history item. Comments and exact evidence text are not rendered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: UUID
    proposal_id: UUID
    review_revision: int
    supersedes_review_id: UUID | None
    reviewer_ref: str
    request_id: UUID
    request_sha256: str
    review_status: ReviewStatus
    proposed_total_score: Decimal | None
    reviewed_total_score: Decimal | None
    criterion_scores: list[GradeReviewHistoryCriterion] = Field(default_factory=list)
    review_reason_codes: list[ReviewReasonCode]
    review_comment_present: bool
    source_status: Literal["current", "source_stale", "unavailable"]
    created_at: datetime
    reviewer_identity_authenticated: Literal[False] = False
    grade_published: Literal[False] = False


class GradeReviewRecordSummary(BaseModel):
    """Safe record result without comments, rationales, and exact answer quotes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    review_id: UUID
    proposal_id: UUID
    review_revision: int
    supersedes_review_id: UUID | None
    review_status: ReviewStatus
    proposed_total_score: Decimal | None
    reviewed_total_score: Decimal | None
    changed_criterion_count: int = Field(ge=0, le=100)
    created: bool
    grade_published: Literal[False] = False


def grade_review_request_sha256(
    *,
    proposal_id: UUID,
    request_id: UUID,
    expected_revision: int,
    payload: GradeReviewDecisionCreate,
    reviewer_user_id: UUID | None = None,
) -> str:
    semantic_request = {
        "proposal_id": str(proposal_id),
        "request_id": str(request_id),
        "expected_revision": expected_revision,
        "payload": payload.model_dump(mode="json"),
    }
    # Preserve every legacy CLI digest; only authenticated HTTP requests add actor identity.
    if reviewer_user_id is not None:
        semantic_request["reviewer_user_id"] = str(reviewer_user_id)
    return hashlib.sha256(canonical_json_bytes(semantic_request)).hexdigest()
