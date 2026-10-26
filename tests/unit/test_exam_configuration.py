"""Pure contracts; none of these tests claims to replace actual PostgreSQL authorization."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.exam_configuration.schemas import (
    ConfigurationCandidateRequest,
    ConfigurationRosterCreateBody,
    ConfigurationSessionCreateBody,
)
from huipi_cloud.modules.exam_configuration.service import allowed


def test_multiple_grants_cannot_create_combined_roster_capability():
    sid = uuid4()
    scope = SimpleNamespace(kind="exam_session", exam_session_id=sid)
    values = [(scope, {"exam.roster.read"}), (scope, {"exam.roster.manage"})]
    assert not allowed(values, {"exam.roster.read", "exam.roster.manage"}, "exam_session", sid)
    assert allowed(
        [(scope, {"exam.roster.read", "exam.roster.manage"})],
        {"exam.roster.read", "exam.roster.manage"},
        "exam_session",
        sid,
    )
    assert not allowed(values, {"exam.roster.read"}, "exam_session", uuid4())


def test_candidate_classes_bounded_unique_and_no_scope_claims():
    cid = uuid4()
    with pytest.raises(ValidationError):
        ConfigurationCandidateRequest(
            request_id=uuid4(), class_ids=[cid, cid], expected_session_revision=1
        )
    with pytest.raises(ValidationError):
        ConfigurationCandidateRequest(
            request_id=uuid4(), class_ids=[cid], expected_session_revision=1, scope="installation"
        )


def test_roster_retains_unknown_variant_and_does_not_accept_client_identity_labels():
    values = dict(
        request_id=uuid4(),
        business_date="2026-11-01",
        reason="合成",
        entries=[dict(student_id=uuid4(), exam_number=" n001 ")],
    )
    result = ConfigurationRosterCreateBody(**values)
    assert result.entries[0].session_paper_id is None and result.entries[0].exam_number == "N001"
    values["entries"][0]["student_name"] = "伪造姓名"
    with pytest.raises(ValidationError):
        ConfigurationRosterCreateBody(**values)


def test_school_session_requires_timezone_and_ordered_dates():
    values = dict(
        request_id=uuid4(),
        subject_id=uuid4(),
        expected_event_revision=1,
        title="合成",
        starts_at="2026-11-01T09:00:00+08:00",
        ends_at="2026-11-01T11:00:00+08:00",
    )
    assert ConfigurationSessionCreateBody(**values).paper_policy == "single"
    with pytest.raises(ValidationError):
        ConfigurationSessionCreateBody(**(values | dict(starts_at="2026-11-01T09:00:00")))
    with pytest.raises(ValidationError):
        ConfigurationSessionCreateBody(**(values | dict(ends_at="2026-10-31T11:00:00+08:00")))
