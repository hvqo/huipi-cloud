"""HTTP version preconditions and identity exclusion do not change the 1.0 CLI DTO."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.answer_review.http_schemas import ReviewRequest
from huipi_cloud.modules.answer_review.protocol import AnswerReviewDecisionCreate


def body():
    return dict(
        request_id=uuid4(),
        expected_revision=0,
        expected_source=dict(
            canonical_document_id=uuid4(),
            canonical_sha256="a" * 64,
            alignment_artifact_id=uuid4(),
            alignment_sha256="b" * 64,
            aligner_version="1.0",
            assignment_questions_digest="c" * 64,
        ),
        decision="uncertain",
        reason_codes=["source_quality_insufficient"],
    )


def test_http_source_and_revision_are_required_and_server_reviewer_is_stable():
    value = body()
    user_id = uuid4()
    request = ReviewRequest(**value)
    assert request.for_reviewer(user_id).reviewer_ref == f"teacher_{user_id.hex}"
    for key in ("expected_source", "expected_revision", "request_id"):
        with pytest.raises(ValidationError):
            ReviewRequest(**{k: v for k, v in value.items() if k != key})
    with pytest.raises(ValidationError):
        ReviewRequest(**{**value, "expected_revision": True})


def test_legacy_protocol_identity_and_shape_still_validate_unchanged():
    payload = AnswerReviewDecisionCreate(
        decision="uncertain",
        reviewer_ref="old-local-reviewer",
        reason_codes=["source_quality_insufficient"],
    )
    assert payload.reviewer_ref == "old-local-reviewer"
    assert "expected_source" not in type(payload).model_fields
    assert "reviewer_user_id" not in type(payload).model_fields
    with pytest.raises(ValidationError):
        ReviewRequest(**{**body(), "reviewer_ref": "self-declared"})


@pytest.mark.parametrize(
    "reason", ["printed_prompt_only", "blank_response_area", "shared_figure_attribution_unclear"]
)
def test_http_cannot_confirm_a_response_while_declaring_prompt_blank_or_shared_ambiguity(reason):
    value = body()
    value.update(
        decision="response_present",
        reason_codes=[reason],
        response_regions=[
            {
                "content_pointer": "/pages/0/blocks/0/content",
                "text_start": 0,
                "text_end": 3,
            }
        ],
    )
    with pytest.raises(ValidationError):
        ReviewRequest(**value)
