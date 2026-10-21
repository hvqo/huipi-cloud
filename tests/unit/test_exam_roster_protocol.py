"""Offline roster input contract: no client-derived identity or implicit variants."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.exam_rosters.permissions import ROSTER_ACTIONS
from huipi_cloud.modules.exam_rosters.schemas import EntrySelection, RosterCorrection, RosterCreate
from huipi_cloud.modules.resource_security.service import CATALOG


def payload():
    return dict(
        request_id=uuid4(),
        exam_session_id=uuid4(),
        business_date="2026-11-01",
        reason="合成",
        entries=[dict(student_id=uuid4(), exam_number=" a-01 ")],
    )


def test_number_normalization_and_unknown_variant_not_inferred():
    r = RosterCreate(**payload())
    assert r.entries[0].exam_number == "A-01" and r.entries[0].session_paper_id is None


@pytest.mark.parametrize(
    "extra",
    [{"role": "admin"}, {"teacher_id": str(uuid4())}, {"is_current": True}, {"status": "frozen"}],
)
def test_client_authority_fields_rejected(extra):
    with pytest.raises(ValidationError):
        RosterCreate(**dict(payload(), **extra))


@pytest.mark.parametrize(
    "extra",
    [
        {"student_name": "伪造"},
        {"class_id": str(uuid4())},
        {"exam_number": "中文号"},
        {"variant_code": "A"},
        {"request_id": str(uuid4())},
    ],
)
def test_no_free_names_class_claims_or_guessed_variant(extra):
    with pytest.raises(ValidationError):
        EntrySelection(**dict(student_id=uuid4(), **extra))


@pytest.mark.parametrize("same_student", [True, False])
def test_duplicate_student_or_case_normalized_exam_number_rejected(same_student):
    a = dict(student_id=uuid4(), exam_number="a01")
    b = dict(
        student_id=a["student_id"] if same_student else uuid4(),
        exam_number="B02" if same_student else "A01",
    )
    with pytest.raises(ValidationError):
        RosterCreate(**dict(payload(), entries=[a, b]))


def test_correction_requires_complete_explicit_entries():
    with pytest.raises(ValidationError):
        RosterCorrection(
            request_id=uuid4(), roster_revision_id=uuid4(), expected_revision=1, reason="更正"
        )
    assert ROSTER_ACTIONS.items() <= CATALOG.items()
    assert all(
        kinds == {"installation", "exam_event", "exam_session"} for kinds in ROSTER_ACTIONS.values()
    )
