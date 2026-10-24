"""Exam processing contracts preserve strict P3 schemas and typed scopes."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.exam_grading.models import (
    ExamGradingOperation,
    ExamGradingWorkspace,
    ExamScoringJob,
)
from huipi_cloud.modules.exam_grading.permissions import MARK_ACTIONS
from huipi_cloud.modules.exam_grading.schemas import AnswerDecision, Intent, ScoreRequest
from huipi_cloud.modules.resource_security.visibility import rules


@pytest.mark.parametrize(
    "key", ["submission_id", "paper_version_id", "student_id", "object_key", "role", "provider"]
)
def test_no_client_source_or_provider_override(key):
    with pytest.raises(ValidationError):
        ScoreRequest(request_id=uuid4(), expected_answer_revision=1, **{key: str(uuid4())})


def test_explicit_revision_and_typed_scope():
    with pytest.raises(ValidationError):
        ScoreRequest(request_id=uuid4(), expected_answer_revision="1")
    assert set(MARK_ACTIONS) == {
        "exam.mark.read",
        "exam.answer.review",
        "exam.score.request",
        "exam.score.review",
    }
    assert all(v == {"installation", "exam_event", "exam_session"} for v in MARK_ACTIONS.values())
    assert {ExamGradingWorkspace, ExamScoringJob, ExamGradingOperation} <= {m for m, _ in rules()}


def test_manual_attribution_is_never_an_implicit_model_decision():
    payload = dict(
        decision="uncertain",
        reason_codes=["source_quality_insufficient"],
        reviewer_ref="teacher_synthetic",
    )
    body = AnswerDecision(request_id=uuid4(), expected_revision=0, payload=payload)
    assert not body.manual_attribution
    with pytest.raises(ValidationError):
        AnswerDecision(
            request_id=uuid4(), expected_revision=0, payload=payload, manual_attribution="true"
        )
    with pytest.raises(ValidationError):
        Intent(request_id=uuid4(), include_protected=True)
