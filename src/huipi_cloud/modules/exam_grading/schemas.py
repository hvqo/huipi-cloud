from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from huipi_cloud.modules.answer_review.protocol import (
    AnswerReviewDecisionCreate,
    AnswerReviewDecisionRead,
)
from huipi_cloud.modules.answer_scripts.schemas import SourcePage
from huipi_cloud.modules.grading.grade_review_protocol import (
    GradeReviewDecisionCreate,
    GradeReviewRevisionDocument,
)
from huipi_cloud.modules.grading.proposal_protocol import GradeProposalDocument
from huipi_cloud.modules.grading.protocol import GradeInputDocument

from .visual_protocol import (
    VisualGradeInput,
    VisualProposal,
    VisualRegion,
    VisualReview,
    VisualReviewDecision,
)


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class AnswerDecision(Intent):
    manual_attribution: bool = Field(default=False, strict=True)
    expected_revision: int = Field(ge=0, le=100, strict=True)
    payload: AnswerReviewDecisionCreate


class ScoreRequest(Intent):
    expected_answer_revision: int = Field(ge=1, le=100, strict=True)


class ScoreReview(Intent):
    expected_revision: int = Field(ge=0, le=100, strict=True)
    payload: GradeReviewDecisionCreate


class VisualRectangle(BaseModel):
    """Coordinates are ten-thousandths of the verified preview dimensions."""

    model_config = ConfigDict(extra="forbid")
    scan_page_id: UUID
    x0: int = Field(ge=0, le=9999, strict=True)
    y0: int = Field(ge=0, le=9999, strict=True)
    x1: int = Field(ge=1, le=10000, strict=True)
    y1: int = Field(ge=1, le=10000, strict=True)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("empty rectangle")
        return self


class VisualSelectionRequest(Intent):
    expected_revision: int = Field(ge=0, le=99, strict=True)
    action: Literal["select", "clear"]
    regions: list[VisualRectangle] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (self.action == "select") != bool(self.regions):
            raise ValueError("select requires image regions; clear has none")
        return self


class VisualSelectionRead(BaseModel):
    id: UUID
    revision: int
    active: bool
    regions: list[VisualRegion]


class VisualScoreRequest(Intent):
    expected_selection_revision: int = Field(ge=1, le=100, strict=True)


class VisualScoreReview(Intent):
    expected_revision: int = Field(ge=0, le=100, strict=True)
    payload: VisualReviewDecision


class MarkQuestion(BaseModel):
    id: UUID
    number: int
    stem: str
    max_score: str


class MarkRubric(BaseModel):
    criterion_id: UUID
    description: str
    points: str


class MarkNode(BaseModel):
    content_pointer: str
    page_index: int
    source_block_id: UUID
    text: str
    normalized_type: str


class ExamMarkDetails(BaseModel):
    workspace_id: UUID
    question: MarkQuestion
    answer_key: str | None
    rubric: list[MarkRubric]
    nodes: list[MarkNode]
    source_pages: list[SourcePage]
    alignment: dict
    answer_review: AnswerReviewDecisionRead | None
    visual_selection: VisualSelectionRead | None = None
    scoring_job_id: UUID | None
    scoring_status: str
    failure_code: str | None
    proposal: GradeProposalDocument | None
    grade_input: GradeInputDocument | None
    history: list[GradeReviewRevisionDocument]
    visual_proposal: VisualProposal | None = None
    visual_grade_input: VisualGradeInput | None = None
    visual_history: list[VisualReview] = Field(default_factory=list)
    history_has_more: bool
