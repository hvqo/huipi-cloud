"""Bounded request and response contracts for teacher-controlled OMR."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TemplateCreate(Strict):
    request_id: UUID
    mapping_ids: list[UUID] = Field(min_length=1, max_length=30)


class RecognitionCreate(Strict):
    request_id: UUID
    template_id: UUID
    scan_page_id: UUID
    expected_revision: int = Field(ge=1)


class ChoiceReview(Strict):
    mapping_id: UUID
    expected_revision: int = Field(ge=0)
    choice: Literal["A", "B", "C", "D", "blank"]
    reason: str = Field(min_length=1, max_length=300)


class ConfirmRequest(Strict):
    request_id: UUID
    decisions: list[ChoiceReview] = Field(min_length=1, max_length=30)


class TemplateQuestion(Strict):
    mapping_id: UUID
    printed_label: str
    correct_choice: str
    max_points: str


class TemplateRead(Strict):
    template_id: UUID | None
    paper_version_id: UUID
    title: str
    questions: list[TemplateQuestion]


class RecognitionQuestion(Strict):
    mapping_id: UUID
    label: str
    status: Literal["single", "blank", "multiple", "uncertain"]
    choice: str | None
    ratios: list[float]
    correct_choice: str
    suggested_points: str | None
    confirmed_choice: str | None = None
    confirmed_points: str | None = None
    revision: int = 0


class MachineQuestion(Strict):
    mapping_id: UUID
    label: str
    status: Literal["single", "blank", "multiple", "uncertain"]
    choice: Literal["A", "B", "C", "D"] | None
    ratios: list[float] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def valid_choice(self):
        if (self.status == "single") != (self.choice is not None):
            raise ValueError("invalid recognition choice")
        if any(not 0 <= value <= 1 for value in self.ratios):
            raise ValueError("invalid recognition ratio")
        return self


class RecognitionRead(Strict):
    recognition_id: UUID
    template_id: UUID
    script_id: UUID
    script_version_id: UUID
    scan_page_id: UUID
    questions: list[RecognitionQuestion]


class WriteReceipt(Strict):
    id: UUID


class CurrentRecognition(Strict):
    id: UUID | None
    template_id: UUID | None
    paper_version_id: UUID
    script_version_id: UUID
    revision: int
