"""Keep a review's selected source separate from other Questions' regions."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from huipi_cloud.modules.answer_review.errors import AnswerReviewRegionInvalidError
from huipi_cloud.modules.answer_review.protocol import AnswerReviewDecisionCreate
from huipi_cloud.modules.answer_review.service import _validate_question_ownership


def _payload(decision: str, pointer: str, *, role: str, reason: str):
    return AnswerReviewDecisionCreate(
        decision=decision,
        response_regions=[{"content_pointer": pointer, "text_start": 0, "text_end": 2}]
        if role == "response"
        else [],
        uncertain_regions=[{"content_pointer": pointer, "text_start": 0, "text_end": 2}]
        if role == "uncertain"
        else [],
        reason_codes=[reason],
        reviewer_ref="local-reviewer",
    )


def test_question_owned_source_cannot_be_confirmed_under_another_question() -> None:
    question_one = uuid4()
    question_two = uuid4()
    pointer = "/pages/0/blocks/4/content"
    answers = [
        SimpleNamespace(question_id=question_one, source_regions=[]),
        SimpleNamespace(
            question_id=question_two,
            source_regions=[SimpleNamespace(content_pointer=pointer, text_start=0, text_end=2)],
        ),
    ]
    payload = _payload(
        "response_present",
        pointer,
        role="response",
        reason="student_work_visible",
    )

    with pytest.raises(AnswerReviewRegionInvalidError):
        _validate_question_ownership(payload, question_one, answers, [])


def test_other_question_source_can_only_be_recorded_as_explicit_uncertainty() -> None:
    question_one = uuid4()
    question_two = uuid4()
    pointer = "/pages/0/blocks/4/content"
    answers = [
        SimpleNamespace(question_id=question_one, source_regions=[]),
        SimpleNamespace(
            question_id=question_two,
            source_regions=[SimpleNamespace(content_pointer=pointer, text_start=0, text_end=2)],
        ),
    ]
    payload = _payload(
        "uncertain",
        pointer,
        role="uncertain",
        reason="response_not_linked_to_question",
    )

    _validate_question_ownership(payload, question_one, answers, [])


def test_unassigned_source_requires_uncertain_response_link_reason() -> None:
    question_id = uuid4()
    pointer = "/pages/0/blocks/4/content"
    answers = [SimpleNamespace(question_id=question_id, source_regions=[])]
    unassigned = [
        SimpleNamespace(
            source_regions=[SimpleNamespace(content_pointer=pointer, text_start=0, text_end=2)]
        )
    ]
    invalid = _payload(
        "uncertain",
        pointer,
        role="uncertain",
        reason="ambiguous_handwriting",
    )

    with pytest.raises(AnswerReviewRegionInvalidError):
        _validate_question_ownership(invalid, question_id, answers, unassigned)


def test_shared_node_with_disjoint_question_ranges_keeps_distinct_ownership() -> None:
    question_one = uuid4()
    question_two = uuid4()
    pointer = "/pages/0/blocks/4/content"
    answers = [
        SimpleNamespace(question_id=question_one, source_regions=[]),
        SimpleNamespace(
            question_id=question_two,
            source_regions=[SimpleNamespace(content_pointer=pointer, text_start=5, text_end=8)],
        ),
    ]
    payload = _payload(
        "response_present",
        pointer,
        role="response",
        reason="student_work_visible",
    )

    _validate_question_ownership(payload, question_one, answers, [])
