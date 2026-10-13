from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.grading.grade_review_protocol import (
    GRADE_REVIEW_SCHEMA_NAME,
    GRADE_REVIEW_SCHEMA_VERSION,
    GradeReviewCriterionInput,
    GradeReviewCriterionReview,
    GradeReviewDecisionCreate,
    GradeReviewRevisionDocument,
)
from huipi_cloud.modules.grading.proposal_protocol import QuoteEvidenceReference


def _payload(**overrides):
    value = {
        "schema_name": GRADE_REVIEW_SCHEMA_NAME,
        "schema_version": GRADE_REVIEW_SCHEMA_VERSION,
        "review_status": "accepted",
        "reviewer_ref": "teacher.demo",
    }
    value.update(overrides)
    return value


def test_review_protocol_requires_a_supported_status_and_explicit_version():
    with pytest.raises(ValidationError):
        GradeReviewDecisionCreate.model_validate({"review_status": "accepted"})
    assert GradeReviewDecisionCreate.model_validate(_payload()).review_status == "accepted"


@pytest.mark.parametrize("value", [-1, 10.5, "NaN", "Infinity"])
def test_reviewed_points_reject_negative_float_and_non_finite_values(value):
    with pytest.raises(ValidationError):
        GradeReviewCriterionInput.model_validate(
            {"criterion_id": uuid4(), "reviewed_points": value}
        )


def test_revised_requires_criterion_reviews_and_non_scored_states_require_reason_codes():
    with pytest.raises(ValidationError):
        GradeReviewDecisionCreate.model_validate(_payload(review_status="revised"))
    with pytest.raises(ValidationError):
        GradeReviewDecisionCreate.model_validate(_payload(review_status="rejected"))
    accepted = GradeReviewDecisionCreate.model_validate(_payload())
    assert accepted.criterion_reviews == []


def test_other_reason_requires_a_comment_and_reason_codes_cannot_repeat():
    with pytest.raises(ValidationError):
        GradeReviewDecisionCreate.model_validate(
            _payload(review_status="rejected", review_reason_codes=["other"])
        )
    with pytest.raises(ValidationError):
        GradeReviewDecisionCreate.model_validate(
            _payload(
                review_status="rejected",
                review_reason_codes=["unsupported_score", "unsupported_score"],
            )
        )


def test_persisted_accepted_revision_cannot_change_a_score():
    criterion_id = uuid4()
    review = GradeReviewCriterionReview(
        criterion_id=criterion_id,
        proposed_points=Decimal("10.00"),
        reviewed_points=Decimal("10.00"),
        max_points=Decimal("10.00"),
        rationale="候选评分符合本项规则。",
        evidence_references=[
            QuoteEvidenceReference(
                kind="quote",
                segment_index=0,
                text_start=0,
                text_end=3,
                quote="答对。",
            )
        ],
        changed=False,
    )
    accepted = GradeReviewRevisionDocument(
        review_id=uuid4(),
        proposal_id=uuid4(),
        grade_input_artifact_id=uuid4(),
        proposal_sha256="a" * 64,
        grade_input_sha256="b" * 64,
        submission_id=uuid4(),
        question_id=uuid4(),
        review_revision=1,
        reviewer_ref="teacher.demo",
        request_id=uuid4(),
        request_sha256="c" * 64,
        review_status="accepted",
        criterion_reviews=[review],
        review_reason_codes=[],
        proposed_total_score=Decimal("10.00"),
        reviewed_total_score=Decimal("10.00"),
        created_at=datetime.now(UTC),
    )
    assert accepted.reviewed_total_score == Decimal("10.00")
    with pytest.raises(ValidationError):
        GradeReviewRevisionDocument.model_validate(
            accepted.model_dump(mode="python")
            | {
                "review_status": "accepted",
                "criterion_reviews": [
                    review.model_copy(update={"reviewed_points": Decimal("8.00"), "changed": True})
                ],
                "reviewed_total_score": Decimal("8.00"),
            }
        )


def test_rejected_candidate_preserves_proposed_total_without_reviewed_total():
    rejected = GradeReviewRevisionDocument(
        review_id=uuid4(),
        proposal_id=uuid4(),
        grade_input_artifact_id=uuid4(),
        proposal_sha256="a" * 64,
        grade_input_sha256="b" * 64,
        submission_id=uuid4(),
        question_id=uuid4(),
        review_revision=1,
        reviewer_ref="teacher.demo",
        request_id=uuid4(),
        request_sha256="c" * 64,
        review_status="rejected",
        criterion_reviews=[],
        review_reason_codes=["unsupported_score"],
        proposed_total_score=Decimal("6.00"),
        reviewed_total_score=None,
        created_at=datetime.now(UTC),
    )
    assert rejected.proposed_total_score == Decimal("6.00")
    assert rejected.reviewed_total_score is None
