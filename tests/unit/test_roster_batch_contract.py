"""Offline bounds and pure assembly; PostgreSQL semantics are tested in integration."""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import insert
from sqlalchemy.dialects.postgresql.asyncpg import dialect

from huipi_cloud.core.config import Settings
from huipi_cloud.modules.exam_rosters import service as rosters
from huipi_cloud.modules.exam_rosters.schemas import EntrySelection


@pytest.mark.parametrize(
    "values",
    [
        {"roster_operation_timeout_seconds": 14},
        {"roster_operation_timeout_seconds": 301},
        {"roster_lock_timeout_seconds": 0},
        {"roster_lock_timeout_seconds": 0.0001},
        {"roster_lock_timeout_seconds": 9},
    ],
)
def test_transaction_budget_configuration_is_bounded(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_assembly_keeps_stable_identity_trusted_labels_and_unknown_variant():
    students = {
        identifier: dict(id=identifier, name="同名", school_student_code=str(identifier))
        for identifier in (uuid4(), uuid4())
    }
    ids = list(students)
    old_participation, classroom_id, enrollment_id = uuid4(), uuid4(), uuid4()
    enrollments = {sid: dict(id=enrollment_id, administrative_class_id=classroom_id) for sid in ids}
    classrooms = {classroom_id: dict(id=classroom_id, display_name="可信班名", code="C1")}
    binding = uuid4()
    selections = [
        EntrySelection(student_id=ids[0], exam_number="S1"),
        EntrySelection(student_id=ids[1], exam_number="S2", session_paper_id=binding),
    ]
    added, snapshots = rosters.snapshot_rows(
        dict(id=uuid4(), exam_session_id=uuid4()),
        selections,
        students,
        enrollments,
        classrooms,
        {ids[0]: old_participation},
    )
    assert len(added) == 1 and added[0]["student_id"] == ids[1]
    assert snapshots[0]["participation_id"] == old_participation
    assert snapshots[1]["participation_id"] == added[0]["id"]
    assert [s["session_paper_id"] for s in snapshots] == [None, binding]
    assert {s["student_name"] for s in snapshots} == {"同名"}
    assert len({s["student_id"] for s in snapshots}) == 2
    assert all(s["class_name"] == "可信班名" for s in snapshots)
    # The actual multi-VALUES parameter count, not an assumed columns * rows estimate.
    batch = [
        dict(snapshots[0], id=uuid4(), student_id=uuid4()) for _ in range(rosters.WRITE_BATCH_SIZE)
    ]
    compiled = insert(rosters.E).values(batch).compile(dialect=dialect())
    assert len(compiled.params) <= 5000 < 32767
