"""Strict CLI/service requests. No classification, actor or arbitrary scope claims."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from huipi_cloud.modules.assignments.schemas import QuestionCreate, RubricReplace


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID


class SnapshotCreate(Request):
    source_assignment_id: UUID
    title: str = Field(min_length=1, max_length=200)


class VersionWrite(Request):
    paper_version_id: UUID
    expected_revision: int = Field(ge=1)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_mapping_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class QuestionEdit(VersionWrite):
    question_key: UUID
    question: QuestionCreate
    answer_content: str = Field(min_length=1, max_length=50000)
    rubric: RubricReplace

    @model_validator(mode="after")
    def total(self):
        if sum((c.points for c in self.rubric.criteria), Decimal(0)) != self.question.max_score:
            raise ValueError("rubric points must equal max_score")
        return self


class MappingOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question_key: UUID
    printed_label: str = Field(min_length=1, max_length=80)


class Reorder(VersionWrite):
    mappings: list[MappingOrder] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique(self):
        for field in ("question_key", "printed_label"):
            values = [getattr(m, field) for m in self.mappings]
            if len(set(values)) != len(values):
                raise ValueError("duplicate mapping identity/label")
        return self
