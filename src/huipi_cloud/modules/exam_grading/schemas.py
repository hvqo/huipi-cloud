from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
    scoring_job_id: UUID | None
    scoring_status: str
    failure_code: str | None
    proposal: GradeProposalDocument | None
    grade_input: GradeInputDocument | None
    history: list[GradeReviewRevisionDocument]
    history_has_more: bool
