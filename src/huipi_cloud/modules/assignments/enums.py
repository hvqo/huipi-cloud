"""Assignment-domain values shared by validation and persistence."""

from enum import StrEnum


class AssignmentStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class QuestionType(StrEnum):
    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"
    FILL_BLANK = "fill_blank"
    SHORT_ANSWER = "short_answer"
    ESSAY = "essay"


class AnswerSource(StrEnum):
    TEACHER = "teacher"
    MODEL_GENERATED = "model_generated"
