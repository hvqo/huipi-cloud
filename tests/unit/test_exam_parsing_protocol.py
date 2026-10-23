"""Protected input contract, fixed scopes and digest checks without DB or model calls."""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from huipi_cloud.modules.answer_scripts.service import manifest_digest, verify_manifest
from huipi_cloud.modules.exam_parsing import service
from huipi_cloud.modules.exam_parsing.permissions import PARSE_ACTIONS
from huipi_cloud.modules.exam_parsing.schemas import ParseRequest
from huipi_cloud.modules.identity.protocol import AccessError


@pytest.mark.parametrize("field", ["student_id", "object_key", "manifest", "sha256", "role"])
def test_start_accepts_no_client_source_or_authority(field):
    with pytest.raises(ValidationError):
        ParseRequest(request_id=uuid4(), expected_revision=2, **{field: str(uuid4())})


def test_start_requires_known_revision_and_explicit_request():
    with pytest.raises(ValidationError):
        ParseRequest(request_id=uuid4(), expected_revision="2")
    with pytest.raises(ValidationError):
        ParseRequest(request_id=uuid4(), expected_revision=0)


def test_permissions_only_use_existing_typed_scopes():
    assert set(PARSE_ACTIONS) == {"exam.parse.start", "exam.parse.read"}
    assert all(k == {"installation", "exam_event", "exam_session"} for k in PARSE_ACTIONS.values())


def test_digest_is_recomputed_not_just_length_checked():
    rows = [
        dict(
            id=uuid4(),
            file_id=uuid4(),
            batch_id=uuid4(),
            page_index=2,
            sha256="a" * 64,
            file_sha256="b" * 64,
        )
    ]
    verify_manifest(dict(manifest_sha256=manifest_digest(rows)), rows)
    with pytest.raises(AccessError) as error:
        verify_manifest(dict(manifest_sha256="0" * 64), rows)
    assert error.value.code == "script_manifest_mismatch"


def test_worker_lease_locks_do_not_block_foreign_key_audit_inserts():
    sql = str(
        select(service.J)
        .with_for_update(skip_locked=True, key_share=True)
        .compile(dialect=postgresql.dialect())
    )
    assert "FOR NO KEY UPDATE SKIP LOCKED" in sql


def test_worker_cli_does_not_log_exception_payload(monkeypatch, caplog):
    from huipi_cloud.workers import exam_parsing_worker

    def failure(coroutine):
        coroutine.close()
        raise RuntimeError("private-object-location-must-not-be-logged")

    monkeypatch.setattr(exam_parsing_worker.asyncio, "run", failure)
    monkeypatch.setattr(exam_parsing_worker, "configure_logging", lambda *args: None)
    with pytest.raises(SystemExit) as error:
        exam_parsing_worker.main()
    assert error.value.code == 1
    assert "RuntimeError" in caplog.text
    assert "private-object-location" not in caplog.text
