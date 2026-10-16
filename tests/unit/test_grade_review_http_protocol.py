"""HTTP actor cannot be self-declared; legacy digest remains byte compatible."""

import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.grading.grade_review_http import GradeReviewRequest
from huipi_cloud.modules.grading.grade_review_protocol import grade_review_request_sha256
from huipi_cloud.modules.grading.protocol import canonical_json_bytes


def test_http_requires_expected_revision_and_derives_compatible_actor():
    uid, rid = uuid4(), uuid4()
    body = {"request_id": rid, "review_status": "accepted", "expected_revision": 0}
    request = GradeReviewRequest.model_validate(body)
    payload = request.for_reviewer(uid)
    assert payload.reviewer_ref == f"teacher_{uid.hex}"
    assert len(payload.reviewer_ref) == 40
    for invalid in [
        {k: v for k, v in body.items() if k != "expected_revision"},
        {**body, "expected_revision": True},
        {**body, "expected_revision": -1},
    ]:
        with pytest.raises(ValidationError):
            GradeReviewRequest.model_validate(invalid)


def test_authenticated_request_digest_adds_actor_without_changing_cli_digest():
    uid, pid, rid = uuid4(), uuid4(), uuid4()
    payload = GradeReviewRequest(
        request_id=rid, expected_revision=0, review_status="accepted"
    ).for_reviewer(uid)
    args = dict(proposal_id=pid, request_id=rid, expected_revision=0, payload=payload)
    original = {
        "proposal_id": str(pid),
        "request_id": str(rid),
        "expected_revision": 0,
        "payload": payload.model_dump(mode="json"),
    }
    assert (
        grade_review_request_sha256(**args)
        == hashlib.sha256(canonical_json_bytes(original)).hexdigest()
    )
    assert grade_review_request_sha256(**args, reviewer_user_id=uid) != grade_review_request_sha256(
        **args
    )
    assert grade_review_request_sha256(**args, reviewer_user_id=uid) != grade_review_request_sha256(
        **args, reviewer_user_id=uuid4()
    )
