"""Strict inputs for teacher-provisioned homework."""

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from huipi_cloud.modules.assignments.schemas import AnswerKeyReplace, QuestionCreate, RubricReplace


class TeachingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    teacher_id: UUID
    class_id: UUID
    term_id: UUID
    subject_id: UUID


class OfferingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    term_id: UUID
    subject_id: UUID
    class_ids: list[UUID] = Field(min_length=1, max_length=20)
    due_at: AwareDatetime

    @model_validator(mode="after")
    def unique_classes(self):
        if len(set(self.class_ids)) != len(self.class_ids):
            raise ValueError("班级不能重复")
        return self


class HomeworkScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class HomeworkQuestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: QuestionCreate
    answer: AnswerKeyReplace
    rubric: RubricReplace

    @model_validator(mode="after")
    def complete_rubric(self):
        if sum(item.points for item in self.rubric.criteria) != self.question.max_score:
            raise ValueError("评分项总分必须等于题目满分")
        return self
