"""Offline contracts for real exam scope, strict authority fields and aware times."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.exams.permissions import EXAM_ACTIONS
from huipi_cloud.modules.exams.schemas import EventCreate, PaperBind, SessionCreate
from huipi_cloud.modules.resource_security.service import CATALOG


def session_payload():
    return dict(
        request_id=uuid4(),
        exam_event_id=uuid4(),
        subject_id=uuid4(),
        expected_event_revision=1,
        title="合成数学",
        starts_at="2026-11-01T09:00:00+08:00",
        ends_at="2026-11-01T11:00:00+08:00",
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"role": "admin"},
        {"teacher_id": str(uuid4())},
        {"academic_term_id": str(uuid4())},
        {"lifecycle": "ready"},
    ],
)
def test_no_client_identity_or_redundant_term_or_ready_claim(extra):
    with pytest.raises(ValidationError):
        SessionCreate(**dict(session_payload(), **extra))


@pytest.mark.parametrize(
    "change",
    [
        {"starts_at": "2026-11-01T09:00:00"},
        {"ends_at": "2026-11-01T09:00:00+08:00"},
        {"paper_policy": "auto"},
        {"expected_event_revision": 0},
        {"subject_id": "math"},
    ],
)
def test_aware_times_order_policy_and_fk_uuid(change):
    with pytest.raises(ValidationError):
        SessionCreate(**dict(session_payload(), **change))


def test_same_instant_different_timezone_and_unicode_title():
    request = SessionCreate(**dict(session_payload(), starts_at="2026-11-01T01:00:00Z"))
    assert request.starts_at.hour == 1 and request.title == "合成数学"
    assert request.model_dump(mode="json")["paper_policy"] == "single"


def test_binding_only_explicit_ab_and_full_revision():
    fields = dict(
        request_id=uuid4(), exam_session_id=uuid4(), expected_revision=1, paper_version_id=uuid4()
    )
    assert PaperBind(**fields).variant_code == "A"
    for extra in ({"variant_code": "C"}, {"role": "admin"}, {"scope": str(uuid4())}):
        with pytest.raises(ValidationError):
            PaperBind(**dict(fields, **extra))


def test_permission_catalog_no_future_roster_or_scan_actions():
    assert EXAM_ACTIONS["exam.event.create"] == {"installation"}
    assert EXAM_ACTIONS["paper.use"] == {"installation", "paper_version"}
    assert EXAM_ACTIONS["exam.session.paper.bind"] == {"installation", "exam_event", "exam_session"}
    assert EXAM_ACTIONS.items() <= CATALOG.items()
    assert "exam.participation.manage" not in CATALOG and "scan.upload" not in CATALOG
    with pytest.raises(ValidationError):
        EventCreate(
            request_id=uuid4(), academic_term_id=uuid4(), title="考试", organizer_user_id=uuid4()
        )
