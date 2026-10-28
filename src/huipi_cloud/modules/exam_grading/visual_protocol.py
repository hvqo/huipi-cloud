"""Versioned visual scoring contracts; historical text and quote contracts are unchanged."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from huipi_cloud.modules.grading.grade_review_protocol import (
    GradeReviewCriterionInput,
    GradeReviewCriterionReview,
    GradeReviewDecisionCreate,
    GradeReviewRevisionDocument,
)
from huipi_cloud.modules.grading.proposal_protocol import GradeProposalDocument
from huipi_cloud.modules.grading.protocol import (
    AnswerKeySnapshot,
    GradeInputQuestionSnapshot,
    RubricCriterionSnapshot,
    json_sha256,
    rubric_sha256,
)

Score = Annotated[Decimal, Field(ge=0, max_digits=8, decimal_places=2, allow_inf_nan=False)]


class VisualRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    region_index: int = Field(ge=0, le=7)
    scan_page_id: UUID
    scan_file_id: UUID
    source_page_index: int = Field(ge=0)
    page_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    crop_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0, le=4096)
    height: int = Field(gt=0, le=4096)
    x0: int = Field(ge=0)
    y0: int = Field(ge=0)
    x1: int = Field(gt=0)
    y1: int = Field(gt=0)

    @model_validator(mode="after")
    def valid_bounds(self) -> Self:
        if not (self.x0 < self.x1 <= self.width and self.y0 < self.y1 <= self.height):
            raise ValueError("image region is outside its verified source")
        return self


class VisualGradeInputCore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["huipi.grading.visual-input"] = "huipi.grading.visual-input"
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID
    selection_id: UUID
    selection_revision: int = Field(gt=0)
    selection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    submission_id: UUID
    assignment_id: UUID
    question: GradeInputQuestionSnapshot
    canonical_artifact_id: UUID
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alignment_artifact_id: UUID
    alignment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    assignment_questions_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    answer_key: AnswerKeySnapshot
    rubric_snapshot: list[RubricCriterionSnapshot] = Field(min_length=1, max_length=100)
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    regions: list[VisualRegion] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        import hashlib

        if (
            self.answer_key.question_id != self.question.question_id
            or hashlib.sha256(self.answer_key.answer_content.encode("utf-8")).hexdigest()
            != self.answer_key.sha256
        ):
            raise ValueError("answer key mismatch")
        if any(r.question_id != self.question.question_id for r in self.rubric_snapshot):
            raise ValueError("rubric question mismatch")
        if (
            sum((r.points for r in self.rubric_snapshot), Decimal("0.00"))
            != self.question.max_score
        ):
            raise ValueError("rubric total mismatch")
        if rubric_sha256(self.rubric_snapshot) != self.rubric_sha256:
            raise ValueError("rubric digest mismatch")
        if [r.region_index for r in self.regions] != list(range(len(self.regions))):
            raise ValueError("image region order mismatch")
        if json_sha256(self.regions) != self.selection_sha256:
            raise ValueError("image region digest mismatch")
        return self


class VisualGradeInput(VisualGradeInputCore):
    artifact_id: UUID
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        core = VisualGradeInputCore.model_validate(
            self.model_dump(exclude={"artifact_id", "input_sha256", "created_at"})
        )
        if json_sha256(core) != self.input_sha256:
            raise ValueError("visual input digest mismatch")
        return self


class VisualEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["image_region"]
    region_index: int = Field(ge=0, le=7)
    description: str = Field(min_length=1, max_length=1000)

    @field_validator("description")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("visual evidence description is blank")
        return value


class VisualCriterionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: UUID
    awarded_points: Score
    rationale: str = Field(min_length=1, max_length=1600)
    evidence_references: list[VisualEvidence] = Field(min_length=1, max_length=8)


class VisualModelReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Some JSON-mode responses include this descriptive marker inside the object.
    # It carries no scoring information and only this exact value is accepted.
    type: Literal["json_object"] | None = None
    # This is the server-selected model-response contract, not a score or
    # evidence; reject a conflicting version even when the model supplies one.
    schema_version: Literal["1.0"] = "1.0"
    outcome: Literal["candidate", "abstain"]
    total_score: Score | None = None
    criterion_scores: list[VisualCriterionDraft] = Field(default_factory=list, max_length=100)
    abstention_reason: str | None = Field(default=None, max_length=2000)
    reason_codes: list[str] = Field(default_factory=list, max_length=10)
    # In an abstention JSON mode may emit an empty top-level evidence list.
    # It is never a substitute for per-Criterion evidence on a candidate.
    evidence_references: list[VisualEvidence] = Field(default_factory=list, max_length=0)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.outcome == "candidate":
            if (
                self.total_score is None
                or not self.criterion_scores
                or self.abstention_reason
                or "evidence_references" in self.model_fields_set
            ):
                raise ValueError("candidate must include scores and no abstention")
        elif self.total_score is not None or self.criterion_scores or not self.abstention_reason:
            raise ValueError("abstention must have a reason and no scores")
        return self


class VisualCriterionScore(VisualCriterionDraft):
    max_points: Score = Field(gt=0)


class VisualProposal(GradeProposalDocument):
    schema_name: Literal["huipi.grading.visual-proposal"] = "huipi.grading.visual-proposal"
    criterion_scores: list[VisualCriterionScore] = Field(default_factory=list, max_length=100)


class VisualReviewCriterionInput(GradeReviewCriterionInput):
    evidence_references: list[VisualEvidence] | None = Field(default=None, max_length=8)


class VisualReviewDecision(GradeReviewDecisionCreate):
    schema_name: Literal["huipi.grading.visual-review"]
    schema_version: Literal["2.0"]
    criterion_reviews: list[VisualReviewCriterionInput] = Field(
        default_factory=list, max_length=100
    )


class VisualReviewCriterion(GradeReviewCriterionReview):
    evidence_references: list[VisualEvidence] = Field(min_length=1, max_length=8)


class VisualReview(GradeReviewRevisionDocument):
    schema_name: Literal["huipi.grading.visual-review"] = "huipi.grading.visual-review"
    schema_version: Literal["2.0"] = "2.0"
    criterion_reviews: list[VisualReviewCriterion] = Field(max_length=100)
