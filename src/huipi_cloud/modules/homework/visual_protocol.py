"""Source-bound visual selection and mixed-input contracts for homework."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from huipi_cloud.modules.exam_grading.visual_protocol import (
    VisualGradeInputCore,
    VisualReviewCriterionInput,
)
from huipi_cloud.modules.grading.protocol import SelectedAnswerSegment, json_sha256


class Rectangle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    page_index: int = Field(ge=0, le=99)
    x0: int = Field(ge=0, le=9999)
    y0: int = Field(ge=0, le=9999)
    x1: int = Field(ge=1, le=10000)
    y1: int = Field(ge=1, le=10000)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("invalid rectangle")
        return self


class HomeworkVisualSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    expected_revision: int = Field(ge=0, le=99)
    regions: list[Rectangle] = Field(min_length=1, max_length=8)


class HomeworkVisualScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    expected_selection_revision: int = Field(ge=1, le=100)
    include_confirmed_text: bool = False


class HomeworkVisualReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    expected_revision: int = Field(ge=0)
    review_status: Literal["accepted", "revised", "rejected", "regrading_required"]
    review_comment: str | None = Field(default=None, max_length=2000)
    criterion_reviews: list[VisualReviewCriterionInput] = Field(
        default_factory=list, max_length=100
    )


class HomeworkVisualRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    region_index: int = Field(ge=0, le=7)
    submission_file_id: UUID
    source_page_index: int = Field(ge=0, le=99)
    source_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    crop_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0, le=4096)
    height: int = Field(gt=0, le=4096)
    x0: int = Field(ge=0)
    y0: int = Field(ge=0)
    x1: int = Field(gt=0)
    y1: int = Field(gt=0)

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if not (self.x0 < self.x1 <= self.width and self.y0 < self.y1 <= self.height):
            raise ValueError("image region is outside its verified source")
        return self


class HomeworkVisualInputCore(VisualGradeInputCore):
    schema_name: Literal["huipi.grading.homework-visual-input"] = (
        "huipi.grading.homework-visual-input"
    )
    regions: list[HomeworkVisualRegion] = Field(min_length=1, max_length=8)
    canonical_artifact_id: UUID | None = None
    canonical_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    alignment_artifact_id: UUID | None = None
    alignment_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmed_text_input_id: UUID | None = None
    confirmed_text_input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmed_text: list[SelectedAnswerSegment] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def text_source(self) -> Self:
        if bool(self.confirmed_text) != bool(
            self.confirmed_text_input_id and self.confirmed_text_input_sha256
        ):
            raise ValueError("confirmed text must be bound to a trusted input")
        sources = (
            self.canonical_artifact_id,
            self.canonical_sha256,
            self.alignment_artifact_id,
            self.alignment_sha256,
        )
        if any(sources) and not all(sources):
            raise ValueError("canonical and alignment sources must be complete")
        if self.confirmed_text and not all(sources):
            raise ValueError("confirmed text must retain its canonical source")
        return self


class HomeworkVisualInput(HomeworkVisualInputCore):
    artifact_id: UUID
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def verify_digest(self) -> Self:
        core = HomeworkVisualInputCore.model_validate(
            self.model_dump(exclude={"artifact_id", "input_sha256", "created_at"})
        )
        if json_sha256(core) != self.input_sha256:
            raise ValueError("homework visual input digest mismatch")
        return self
