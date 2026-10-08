"""Pydantic schemas for the assignment management API."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from huipi_cloud.modules.assignments.enums import (
    AnswerSource,
    AssignmentStatus,
    QuestionType,
)


class AssignmentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=100)
    grade_level: str = Field(min_length=1, max_length=50)
    description: str | None = None

    @field_validator("title", "subject", "grade_level", mode="before")
    @classmethod
    def trim_required_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class QuestionCreate(BaseModel):
    question_number: int = Field(gt=0)
    question_type: QuestionType
    stem: str = Field(min_length=1, max_length=20000)
    max_score: Decimal = Field(gt=0, max_digits=8, decimal_places=2)

    @field_validator("stem", mode="before")
    @classmethod
    def trim_stem(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AnswerKeyReplace(BaseModel):
    answer_content: str = Field(min_length=1, max_length=50000)

    @field_validator("answer_content", mode="before")
    @classmethod
    def trim_answer(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class RubricCriterionInput(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    points: Decimal = Field(gt=0, max_digits=8, decimal_places=2)
    sort_order: int = Field(ge=1)

    @field_validator("description", mode="before")
    @classmethod
    def trim_description(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class RubricReplace(BaseModel):
    criteria: list[RubricCriterionInput] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def require_unique_sort_order(self) -> "RubricReplace":
        order_values = [criterion.sort_order for criterion in self.criteria]
        if len(order_values) != len(set(order_values)):
            raise ValueError("sort_order 不能重复")
        return self


class AnswerKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    question_id: UUID
    answer_content: str
    source: AnswerSource
    created_at: datetime
    updated_at: datetime


class RubricCriterionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    question_id: UUID
    description: str
    points: Decimal
    sort_order: int


class QuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    assignment_id: UUID
    question_number: int
    question_type: QuestionType
    stem: str
    max_score: Decimal
    created_at: datetime
    updated_at: datetime
    answer_key: AnswerKeyRead | None
    rubric_criteria: list[RubricCriterionRead]


class AssignmentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    subject: str
    grade_level: str
    description: str | None
    status: AssignmentStatus
    created_at: datetime
    updated_at: datetime


class AssignmentDetail(AssignmentSummary):
    questions: list[QuestionRead]


class ErrorResponse(BaseModel):
    detail: str


class RubricRead(BaseModel):
    criteria: list[RubricCriterionRead]
