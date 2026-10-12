"""Strict, versioned protocol for LLM-generated rubric score candidates."""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from huipi_cloud.modules.grading.protocol import canonical_json_bytes

_Score = Annotated[
    Decimal,
    Field(ge=0, max_digits=8, decimal_places=2, allow_inf_nan=False),
]
_ReasonCode = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]


class QuoteEvidenceReference(BaseModel):
    """An exact quote within one SelectedAnswerSegment, using Unicode offsets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["quote"]
    segment_index: int = Field(ge=0)
    text_start: int = Field(ge=0)
    text_end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.text_end <= self.text_start:
            raise ValueError("evidence offsets must define a non-empty half-open range")
        if self.text_end - self.text_start != len(self.quote):
            raise ValueError("evidence quote length must match Unicode code-point offsets")
        return self


class NoEvidenceReference(BaseModel):
    """Explain a missing step; positive scores still require a validated quote."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["no_evidence"]
    reason: str = Field(min_length=1, max_length=1000)


EvidenceReference = Annotated[
    QuoteEvidenceReference | NoEvidenceReference,
    Field(discriminator="kind"),
]


class CriterionScoreDraft(BaseModel):
    """Model-authored score fields; criterion maximums come only from GradeInput."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: UUID
    awarded_points: _Score
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_references: list[EvidenceReference] = Field(min_length=1, max_length=50)


class GradeModelReply(BaseModel):
    """Strict provider JSON contract before deterministic score/evidence checks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"]
    outcome: Literal["candidate", "abstain"]
    total_score: _Score | None = None
    criterion_scores: list[CriterionScoreDraft] = Field(default_factory=list, max_length=100)
    abstention_reason: str | None = Field(default=None, max_length=2000)
    reason_codes: list[_ReasonCode] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.outcome == "candidate":
            if self.total_score is None or not self.criterion_scores:
                raise ValueError("candidate reply requires scores and a total")
            if self.abstention_reason is not None:
                raise ValueError("candidate reply cannot include an abstention reason")
        else:
            if self.total_score is not None or self.criterion_scores:
                raise ValueError("abstention reply cannot include scores")
            if not self.abstention_reason or not self.abstention_reason.strip():
                raise ValueError("abstention reply requires a reason")
        return self


class CriterionScore(BaseModel):
    """Validated criterion score with its maximum copied from the trusted rubric."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: UUID
    awarded_points: _Score
    max_points: Decimal = Field(
        gt=0,
        max_digits=8,
        decimal_places=2,
        allow_inf_nan=False,
    )
    rationale: str = Field(min_length=1, max_length=4000)
    evidence_references: list[EvidenceReference] = Field(min_length=1, max_length=50)


class GradeProposalDocument(BaseModel):
    """Private immutable LLM proposal. It is never a final teacher grade."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["huipi.grading.proposal"] = "huipi.grading.proposal"
    schema_version: Literal["1.0"] = "1.0"
    proposal_id: UUID
    request_id: UUID
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    grade_input_artifact_id: UUID
    grade_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    submission_id: UUID
    question_id: UUID
    model_id: str = Field(min_length=1, max_length=200)
    provider: Literal["ollama_native", "openai_compatible"]
    prompt_version: str = Field(min_length=1, max_length=64)
    generation_config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    criterion_scores: list[CriterionScore] = Field(default_factory=list, max_length=100)
    total_score: _Score | None = None
    max_score: Decimal = Field(
        gt=0,
        max_digits=8,
        decimal_places=2,
        allow_inf_nan=False,
    )
    grading_status: Literal["completed_candidate", "grading_abstained"]
    reason_codes: list[_ReasonCode] = Field(default_factory=list, max_length=10)
    abstention_reason: str | None = Field(default=None, max_length=2000)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    model_elapsed_ms: int = Field(ge=0)
    created_at: datetime

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.grading_status == "completed_candidate":
            if not self.criterion_scores or self.total_score is None:
                raise ValueError("completed candidate requires criterion and total scores")
            if self.abstention_reason is not None:
                raise ValueError("completed candidate cannot include an abstention reason")
            calculated = sum(
                (item.awarded_points for item in self.criterion_scores), Decimal("0.00")
            )
            if calculated != self.total_score or calculated > self.max_score:
                raise ValueError("proposal total must equal its validated criterion scores")
            if len({item.criterion_id for item in self.criterion_scores}) != len(
                self.criterion_scores
            ):
                raise ValueError("proposal criterion IDs must be unique")
            if any(item.awarded_points > item.max_points for item in self.criterion_scores):
                raise ValueError("proposal criterion score cannot exceed its maximum")
            if sum((item.max_points for item in self.criterion_scores), Decimal("0.00")) != (
                self.max_score
            ):
                raise ValueError("proposal criterion maximums must sum to the Question maximum")
        elif (
            self.criterion_scores
            or self.total_score is not None
            or not self.abstention_reason
            or not self.abstention_reason.strip()
        ):
            raise ValueError("abstained proposal cannot contain scores and requires a reason")
        return self


class GradeProposalInspection(BaseModel):
    """Safe CLI summary; it never includes student text or object storage locators."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready"]
    grade_input_artifact_id: UUID
    grade_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    submission_id: UUID
    question_id: UUID
    question_number: int = Field(gt=0)
    rubric_criterion_count: int = Field(ge=1, le=100)
    rubric_total_points: Decimal = Field(gt=0, max_digits=8, decimal_places=2)
    provider: Literal["ollama_native", "openai_compatible"]
    model_id: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=64)
    max_input_bytes: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_attempts: int = Field(ge=1, le=2)


class GradeProposalRunResult(BaseModel):
    """Safe command result with score totals but no rationale or answer quotation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["completed_candidate", "grading_abstained"]
    proposal_id: UUID
    request_id: UUID
    grade_input_artifact_id: UUID
    grade_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    criterion_scores: list[tuple[UUID, Decimal]] = Field(max_length=100)
    total_score: Decimal | None = None
    max_score: Decimal
    reason_codes: list[str] = Field(default_factory=list, max_length=10)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    model_elapsed_ms: int = Field(ge=0)
    created: bool


def proposal_json_bytes(value: object) -> bytes:
    return canonical_json_bytes(value)


def proposal_sha256(value: object) -> str:
    return hashlib.sha256(proposal_json_bytes(value)).hexdigest()
