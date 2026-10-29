"""Pure contract checks for the teacher-scoped homework entry points."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.homework.schemas import (
    HomeworkQuestionCreate,
    OfferingCreate,
    TeachingCreate,
)
from huipi_cloud.modules.homework.service import grade_input_error_code


def test_homework_question_requires_complete_exact_rubric():
    payload = {
        "question": {
            "question_number": 1,
            "question_type": "short_answer",
            "stem": "计算",
            "max_score": "10.00",
        },
        "answer": {"answer_content": "4"},
        "rubric": {
            "criteria": [
                {"description": "步骤", "points": "4.25", "sort_order": 1},
                {"description": "结果", "points": "5.75", "sort_order": 2},
            ]
        },
    }
    assert (
        HomeworkQuestionCreate.model_validate(payload).question.max_score.as_tuple().exponent == -2
    )
    payload["rubric"]["criteria"][1]["points"] = "5.74"
    with pytest.raises(ValidationError):
        HomeworkQuestionCreate.model_validate(payload)


def test_homework_offering_rejects_duplicate_class_and_naive_deadline():
    term, subject, classroom = uuid4(), uuid4(), uuid4()
    base = dict(
        term_id=term, subject_id=subject, class_ids=[classroom], due_at="2026-11-01T10:00:00+08:00"
    )
    OfferingCreate.model_validate(base)
    with pytest.raises(ValidationError):
        OfferingCreate.model_validate({**base, "class_ids": [classroom, classroom]})
    with pytest.raises(ValidationError):
        OfferingCreate.model_validate({**base, "due_at": "2026-11-01T10:00:00"})


def test_client_cannot_claim_admin_or_choose_extra_scope_on_teaching_grant():
    identifiers = {key: uuid4() for key in ("teacher_id", "class_id", "term_id", "subject_id")}
    TeachingCreate.model_validate(identifiers)
    with pytest.raises(ValidationError):
        TeachingCreate.model_validate({**identifiers, "is_admin": True})


def test_visual_evidence_is_not_reported_as_missing_teacher_confirmation():
    assert grade_input_error_code("unsupported_modality") == "homework_visual_scoring_not_supported"
    assert grade_input_error_code("review_required") == "homework_answer_not_ready"
