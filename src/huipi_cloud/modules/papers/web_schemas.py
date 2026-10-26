"""Minimal teacher projections; protected carrier IDs are never web contracts."""

from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from huipi_cloud.modules.assignments.enums import QuestionType
from huipi_cloud.modules.assignments.schemas import QuestionCreate, RubricReplace
from huipi_cloud.modules.papers.schemas import MappingOrder, QuestionEdit, Reorder, Request


class PaperWebChecks(BaseModel):
    questions_complete: bool
    answers_complete: bool
    rubric_complete: bool
    mapping_complete: bool
    content_current: bool


class PaperWebSummary(BaseModel):
    id: UUID
    family_id: UUID
    prior_version_id: UUID | None
    version_no: int
    revision: int
    title: str
    status: str
    question_count: int
    max_points: str
    expected_content_sha256: str
    expected_mapping_digest: str
    checks: PaperWebChecks
    usable_for_exam: bool
    actions: list[str]


class PaperWebPage(BaseModel):
    items: list[PaperWebSummary]
    has_more: bool
    can_create: bool


class PaperWebQuestionValue(BaseModel):
    question_number: int
    question_type: QuestionType
    stem: str
    max_score: str


class PaperWebCriterion(BaseModel):
    description: str
    points: str
    sort_order: int


class PaperWebRubric(BaseModel):
    criteria: list[PaperWebCriterion]


class PaperWebQuestion(BaseModel):
    question_key: UUID
    printed_label: str
    sort_order: int
    question: PaperWebQuestionValue
    answer_content: str
    rubric: PaperWebRubric


class PaperWebDetail(BaseModel):
    paper: PaperWebSummary
    questions: list[PaperWebQuestion]


class PaperWebSource(BaseModel):
    id: UUID
    title: str
    subject: str
    grade_level: str


class PaperWebSources(BaseModel):
    items: list[PaperWebSource]
    has_more: bool


class PaperWebWrite(Request):
    expected_revision: int = Field(ge=1)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_mapping_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PaperWebQuestionEdit(PaperWebWrite):
    question_key: UUID
    question: QuestionCreate
    answer_content: str = Field(min_length=1, max_length=50000)
    rubric: RubricReplace

    @model_validator(mode="after")
    def total(self):
        return QuestionEdit.total(self)


class PaperWebReorder(PaperWebWrite):
    mappings: list[MappingOrder] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique(self):
        return Reorder.unique(self)


class PaperWebWriteResult(BaseModel):
    paper_version_id: UUID
    revision: int
    status: str
