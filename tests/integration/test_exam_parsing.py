"""Protected parser contract on real PostgreSQL/private S3. Synthetic executor is explicit."""

import asyncio
import hashlib
import json
import os
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from pypdf import PdfReader
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from test_answer_scripts import (  # noqa: F401
    auth_api,
    base,
    candidates,
    confirmation,
    draft,
    exam,
    papers,
    roster,
    scan_context,
    script_context,
)

from huipi_cloud.core.config import Settings
from huipi_cloud.modules.exam_parsing import service
from huipi_cloud.modules.exam_parsing.permissions import PARSE_OPERATOR_ROLE
from huipi_cloud.modules.exam_parsing.runtime import ExamBackend, ProtectedExecutor
from huipi_cloud.modules.parsing import repository as legacy
from huipi_cloud.modules.parsing.executor import ParsedArtifactResult
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityScope
from huipi_cloud.workers.parsing import ParsingWorker


class SyntheticExecutor:
    """No OCR/model: emits explicitly synthetic, protocol-valid page text to real S3."""

    def __init__(self, storage):
        self.storage = storage
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.cancelled = False

    async def execute(self, task):
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        remote = await self.storage.download(task.object_key)
        content = b"".join([b async for b in remote.chunks()])
        count = len(PdfReader(BytesIO(content)).pages)
        middle = dict(
            schema="docvortex.middle",
            schema_version="2.0",
            metadata=dict(
                file_suffix="pdf", producer=dict(name="mineru", version="4.0.10"), document={}
            ),
            extensions=dict(
                mineru=dict(tier="basic", parse_mode="txt", synthetic_test_executor=True)
            ),
            pages=[
                dict(
                    page_idx=i,
                    blocks=[
                        dict(
                            type="text",
                            index=0,
                            content=f"Synthetic executor page {i + 1}: student response 2+2=4",
                        )
                    ],
                )
                for i in range(count)
            ],
            is_full_document=True,
        )
        identifier = uuid4()
        values = dict(
            artifact_id=identifier,
            task_id=task.task_id,
            submission_id=task.submission_id,
            bucket=task.bucket,
            original_sha256=task.sha256,
            parser_name="MinerU",
            parser_version="4.0.10",
            tier="basic",
            schema_name="docvortex.middle",
            schema_version="2.0",
            page_count=count,
            asset_count=0,
        )
        for kind, payload in [
            ("archive", b"synthetic test archive; not real MinerU"),
            ("markdown", b"Synthetic executor student response 2+2=4"),
            ("middle_json", json.dumps(middle).encode()),
            ("structured_content", b"[]"),
            ("assets_manifest", b'{"schema":"huipi.parsed-assets","version":"1","assets":[]}'),
        ]:
            key = f"{self.storage.object_key_prefix}/synthetic-parser/{identifier}/{kind}"
            await self.storage.upload_fileobj(BytesIO(payload), key, "application/octet-stream")
            values.update(
                {
                    kind + "_key": key,
                    kind + "_size_bytes": len(payload),
                    kind + "_sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        return ParsedArtifactResult(**values)


@pytest.fixture
async def parse_context(script_context):  # noqa: F811
    c = script_context
    async with c.factory() as db:
        await service.enable_operator(
            db,
            c.users["admin"].id,
            c.users["admin"].id,
            uuid4(),
            "synthetic protected parser deployment",
        )
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        c.parse_grant = await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=PARSE_OPERATOR_ROLE,
            scope_id=scope,
        )
    c.candidates = await candidates(c)
    c.answer = (await draft(c)).json()
    body = confirmation(c, c.answer)
    body["page_ids"] = list(reversed(body["page_ids"]))
    response = await c.client.post(
        f"/api/v1/answer-scripts/{c.answer['script_id']}/versions", json=body
    )
    assert response.status_code == 200, response.text
    c.answer = response.json()
    c.config = Settings(
        _env_file=None,
        parsing_lease_seconds=180,
        parsing_heartbeat_seconds=1,
        parsing_poll_seconds=0.05,
        parsing_execution_timeout_seconds=240,
        parsing_cancel_grace_seconds=1,
    )
    c.backend = ExamBackend(c.storage, c.config)
    c.parser = SyntheticExecutor(c.storage)
    c.executor = ProtectedExecutor(c.parser, c.backend)
    c.worker = ParsingWorker(c.factory, c.executor, c.config, repository_backend=c.backend)
    try:
        yield c
    finally:
        async with c.factory() as db, db.begin():
            await db.execute(
                text(
                    "TRUNCATE exam_parsing_operations,exam_submission_links,"
                    "exam_parsing_jobs CASCADE"
                )
            )


async def start(c, rid=None, revision=None):
    return await c.client.post(
        f"/api/v1/answer-scripts/{c.answer['script_id']}/parsing",
        json=dict(
            request_id=str(rid or uuid4()), expected_revision=revision or c.answer["revision"]
        ),
    )


async def consume(c):
    claimed = await c.backend.claim_next_task(c.factory, lease_seconds=180)
    assert claimed is not None
    await c.worker._execute_claim(claimed)
    return claimed


@pytest.mark.anyio
async def test_ordered_pdf_parse_canonical_and_legacy_isolation(parse_context):
    c = parse_context
    r = await start(c)
    assert r.status_code == 202, r.text
    claim = await c.backend.claim_next_task(c.factory, lease_seconds=180)
    assert claim is not None
    assert await legacy.claim_next_task(c.factory, lease_seconds=60) is None
    async with c.factory() as db:
        link = (await db.execute(select(service.K))).mappings().one()
        file = await service.scripts.exams.row(
            db,
            __import__("huipi_cloud.modules.exam_parsing.runtime", fromlist=["F"]).F,
            link["submission_file_id"],
        )
    original = await c.client.get(f"/api/v1/scan-files/{c.scan_file['id']}/original")
    remote = await c.storage.download(file["object_key"])
    assembled = b"".join([chunk async for chunk in remote.chunks()])
    assert hashlib.sha256(assembled).hexdigest() == file["sha256"]
    original_pages = PdfReader(BytesIO(original.content)).pages
    output_pages = PdfReader(BytesIO(assembled)).pages
    assert [p.extract_text() for p in output_pages] == [
        original_pages[i].extract_text() for i in [1, 0]
    ]
    await c.worker._execute_claim(claim)
    status = await c.client.get(f"/api/v1/exam-parsing-jobs/{r.json()['job_id']}")
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "succeeded" and status.json()["page_count"] == 2
    page = await c.client.get(f"/api/v1/exam-parsing-jobs/{r.json()['job_id']}/pages/0")
    assert page.status_code == 200, page.text
    assert page.json()["source"]["scan_page_id"] == c.page_ids[1]
    assert "Synthetic executor" in page.text
    assert all(k not in page.text + status.text for k in ["object_key", "access_key", "secret_key"])
    for path in [
        f"/submissions/{link['submission_id']}",
        f"/submissions/{link['submission_id']}/file",
        f"/submissions/{link['submission_id']}/canonical-document",
    ]:
        old = await c.client.get("/api/v1" + path)
        assert old.status_code == 404, old.text
    visible = (await c.client.get("/api/v1/assignments")).json()
    from huipi_cloud.modules.exam_parsing.runtime import SUB

    async with c.factory() as db:
        protected_assignment = await db.scalar(
            select(SUB.c.assignment_id).where(SUB.c.id == link["submission_id"])
        )
    assert str(protected_assignment) not in {item["id"] for item in visible}


@pytest.mark.anyio
async def test_repeated_click_and_request_conflict(parse_context):
    c = parse_context
    rid = uuid4()
    a, b = await asyncio.gather(start(c, rid), start(c, rid))
    assert a.status_code == b.status_code == 202, (a.text, b.text)
    assert a.json() == b.json()
    again = await start(c)
    assert again.json() == a.json()
    conflict = await start(c, rid, revision=1)
    assert conflict.status_code == 409
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.J)) == 1


@pytest.mark.anyio
async def test_unclaimed_and_incomplete_cannot_start(parse_context):
    c = parse_context
    body = dict(
        request_id=str(uuid4()),
        expected_revision=2,
        action="save_draft",
        page_ids=c.page_ids[:2],
        completeness="incomplete",
        reason="合成缺页",
    )
    r = await c.client.post(f"/api/v1/answer-scripts/{c.answer['script_id']}/versions", json=body)
    assert r.status_code == 200, r.text
    c.answer = r.json()
    assert (await start(c)).status_code == 409
    body = confirmation(c, c.answer)
    body["completeness"] = "incomplete"
    r = await c.client.post(f"/api/v1/answer-scripts/{c.answer['script_id']}/versions", json=body)
    assert r.status_code == 200, r.text
    c.answer = r.json()
    assert (await start(c)).status_code == 409
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.J)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["script", "roster", "revoke", "retire"])
async def test_running_source_changes_cancel_and_cannot_commit(parse_context, change):
    c = parse_context
    r = await start(c)
    assert r.status_code == 202, r.text
    claimed = await c.backend.claim_next_task(c.factory, lease_seconds=180)
    c.parser.release.clear()
    execution = asyncio.create_task(c.worker._execute_claim(claimed))
    await asyncio.wait_for(c.parser.entered.wait(), 10)
    try:
        if change == "script":
            body = dict(
                request_id=str(uuid4()),
                expected_revision=2,
                action="save_draft",
                page_ids=c.page_ids[:2],
                completeness="complete",
                reason="合成更正",
            )
            assert (
                await c.client.post(
                    f"/api/v1/answer-scripts/{c.answer['script_id']}/versions", json=body
                )
            ).status_code == 200
        elif change == "roster":
            from test_exam_rosters import call, entries, freeze, req

            from huipi_cloud.modules.exam_rosters import service as rosters
            from huipi_cloud.modules.exam_rosters.schemas import RosterCorrection

            new = await call(
                c,
                rosters.correct_roster,
                RosterCorrection(
                    **req(c.frozen_roster).model_dump(),
                    reason="合成更正",
                    entries=entries(c, 4, unknown=True),
                ),
            )
            await freeze(c, new)
        elif change == "revoke":
            async with c.factory() as db:
                await security.revoke_grant(
                    db, c.principals["admin"], uuid4(), grant_id=UUID(c.parse_grant["grant_id"])
                )
        else:
            from test_paper_versions import write

            from huipi_cloud.modules.papers.service import retire_paper

            async with c.factory() as db:
                await retire_paper(db, c.principals["admin"], write(c.paper))
        await asyncio.wait_for(execution, 8)
    finally:
        c.parser.release.set()
        await asyncio.gather(execution, return_exceptions=True)
    assert c.parser.cancelled
    async with c.factory() as db:
        assert await db.scalar(select(service.J.c.status)) == "invalidated"
        assert await db.scalar(select(func.count()).select_from(service.PA)) == 0


@pytest.mark.anyio
async def test_start_audit_failure_rolls_back_job(parse_context):
    c = parse_context
    engine = c.factory.kw["bind"].sync_engine

    def fail(conn, cursor, stmt, params, ctx, many):
        if stmt.startswith("INSERT INTO exam_parsing_operations"):
            raise OperationalError("synthetic", {}, Exception("audit unavailable"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        r = await start(c)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert r.status_code == 503, r.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.J)) == 0


@pytest.mark.anyio
async def test_storage_failure_and_retry_leave_no_partial_carrier(parse_context, monkeypatch):
    from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError

    c = parse_context
    assert (await start(c)).status_code == 202

    async def broken(*args, **kwargs):
        raise StorageUnavailableError("synthetic")

    monkeypatch.setattr(c.storage, "upload_fileobj", broken)
    assert await c.backend.claim_next_task(c.factory, lease_seconds=180) is None
    async with c.factory() as db:
        assert await db.scalar(select(service.J.c.status)) == "retry_wait"
        assert await db.scalar(select(func.count()).select_from(service.K)) == 0
        assert await db.scalar(text("SELECT count(*) FROM submissions")) == 0


@pytest.mark.anyio
async def test_expired_lease_cannot_register_result(parse_context):
    c = parse_context
    assert (await start(c)).status_code == 202
    claim = await c.backend.claim_next_task(c.factory, lease_seconds=180)
    artifact = await c.executor.execute(claim.task)
    async with c.factory() as db, db.begin():
        await db.execute(
            update(service.J)
            .where(service.J.c.id == claim.task.task_id)
            .values(lease_expires_at=func.clock_timestamp() - text("interval '1 second'"))
        )
    assert (
        await c.backend.complete_task_with_artifact(
            c.factory, artifact=artifact, lease_token=claim.lease_token
        )
        is False
    )
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.PA)) == 0


@pytest.mark.anyio
@pytest.mark.mineru_e2e
@pytest.mark.skipif(os.environ.get("MINERU_E2E") != "1", reason="explicit local cached MinerU only")
async def test_real_mineru_protected_input_to_canonical(parse_context):
    import signal
    import sys

    c = parse_context
    r = await start(c)
    assert r.status_code == 202, r.text
    env = dict(
        os.environ,
        DATABASE_URL=os.environ["TEST_DATABASE_URL"],
        MINIO_BUCKET=c.storage.bucket,
        MINIO_OBJECT_KEY_PREFIX=c.storage.object_key_prefix,
        PARSING_POLL_SECONDS="0.1",
        PARSING_HEARTBEAT_SECONDS="2",
        PARSING_EXECUTION_TIMEOUT_SECONDS="240",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "huipi_cloud.workers.exam_parsing_worker",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(120):
            while True:
                result = await c.client.get(f"/api/v1/exam-parsing-jobs/{r.json()['job_id']}")
                assert result.status_code == 200, result.text
                if result.json()["status"] in {"succeeded", "failed", "invalidated"}:
                    break
                if process.returncode is not None:
                    raise AssertionError("standalone exam worker exited before completion")
                await asyncio.sleep(0.2)  # Bounded status polling, never a fake success timer.
        assert result.json()["status"] == "succeeded", result.text
        assert result.json()["page_count"] == 2
        page = await c.client.get(f"/api/v1/exam-parsing-jobs/{r.json()['job_id']}/pages/0")
        assert page.status_code == 200, page.text
        assert "Synthetic executor" not in page.text
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 10)
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = await process.communicate()
        # Child uploads are outside the recorder, so register only this test's unique
        # private prefix for its normal fixture cleanup. No whole-bucket enumeration.
        assert c.storage.object_key_prefix.startswith("tests/")
        listing = await asyncio.to_thread(
            c.storage.storage._client.list_objects_v2,
            Bucket=c.storage.bucket,
            Prefix=c.storage.object_key_prefix + "/",
            MaxKeys=1000,
        )
        assert not listing.get("IsTruncated")
        c.storage.uploaded_keys.update(item["Key"] for item in listing.get("Contents", []))
    assert process.returncode == 0, "standalone worker did not stop gracefully"
    assert b"Traceback" not in output


@pytest.mark.anyio
async def test_direct_sql_wrong_manifest_rejected_before_bridge(parse_context):
    from huipi_cloud.modules.answer_scripts import service as script_service

    c = parse_context
    aid = UUID(c.answer["script_id"])
    vid = uuid4()
    async with c.factory() as db, db.begin():
        old = await script_service.exams.row(db, script_service.V, UUID(c.answer["version_id"]))
        await db.execute(
            text("DELETE FROM current_page_allocations WHERE answer_script_id=:id"), dict(id=aid)
        )
        await db.execute(
            update(script_service.A)
            .where(script_service.A.c.id == aid)
            .values(current_version_id=vid, revision=3)
        )
        values = {k: v for k, v in old.items() if k not in ["id", "creation_txid", "created_at"]}
        values.update(id=vid, version_no=3, previous_version_id=old["id"], manifest_sha256="0" * 64)
        await db.execute(insert(script_service.V).values(**values))
        rows = await script_service.source_rows(
            db, UUID(c.session["id"]), list(map(UUID, reversed(c.page_ids[:2])))
        )
        for i, row in enumerate(rows):
            await db.execute(
                insert(script_service.L).values(
                    version_id=vid,
                    ordinal=i,
                    scan_page_id=row["id"],
                    file_id=row["file_id"],
                    source_page_index=row["page_index"],
                    page_sha256=row["sha256"],
                    file_sha256=row["file_sha256"],
                )
            )
            await db.execute(
                insert(script_service.C).values(
                    scan_page_id=row["id"],
                    answer_script_id=aid,
                    version_id=vid,
                    exam_session_id=UUID(c.session["id"]),
                )
            )
    c.answer["revision"] = 3
    for response in [await c.client.get(f"/api/v1/answer-scripts/{aid}/manifest"), await start(c)]:
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "script_manifest_mismatch"
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.J)) == 0
        assert await db.scalar(select(func.count()).select_from(service.K)) == 0


@pytest.mark.anyio
async def test_commit_ack_loss_replays_start_without_duplicate_job(parse_context):
    from huipi_cloud.infrastructure.database.session import get_db_session
    from huipi_cloud.main import app

    c = parse_context
    rid = uuid4()

    async def lost_session():
        async with c.factory() as db:
            original = db.commit
            count = 0

            async def commit():
                nonlocal count
                count += 1
                await original()
                if count == 4:
                    raise OperationalError("synthetic", {}, Exception("lost ACK"))

            db.commit = commit
            yield db

    previous = app.dependency_overrides[get_db_session]
    app.dependency_overrides[get_db_session] = lost_session
    try:
        response = await start(c, rid)
    finally:
        app.dependency_overrides[get_db_session] = previous
    assert response.status_code == 503, response.text
    replay = await start(c, rid)
    assert replay.status_code == 202, replay.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.J)) == 1
        assert await db.scalar(select(func.count()).select_from(service.OPS)) == 1


@pytest.mark.anyio
async def test_completed_commit_ack_loss_keeps_referenced_objects(parse_context):
    from sqlalchemy.orm import Session

    c = parse_context
    rid = uuid4()
    r = await start(c, rid)
    assert r.status_code == 202
    claim = await c.backend.claim_next_task(c.factory, lease_seconds=180)
    artifact = await c.executor.execute(claim.task)

    def lose(session):
        raise OperationalError("synthetic", {}, Exception("lost final ACK"))

    event.listen(Session, "after_commit", lose)
    try:
        with pytest.raises(OperationalError):
            await c.backend.complete_task_with_artifact(
                c.factory, artifact=artifact, lease_token=claim.lease_token
            )
    finally:
        event.remove(Session, "after_commit", lose)
    result = await c.client.get(f"/api/v1/exam-parsing-jobs/{r.json()['job_id']}")
    assert result.json()["status"] == "succeeded", result.text
    assert (await start(c, rid)).json() == r.json()
    page = await c.client.get(f"/api/v1/exam-parsing-jobs/{r.json()['job_id']}/pages/0")
    assert page.status_code == 200, page.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.PA)) == 1


@pytest.mark.anyio
async def test_completion_audit_error_rolls_back_both_artifact_indexes(parse_context):
    c = parse_context
    assert (await start(c)).status_code == 202
    claim = await c.backend.claim_next_task(c.factory, lease_seconds=180)
    artifact = await c.executor.execute(claim.task)
    engine = c.factory.kw["bind"].sync_engine

    def fail(conn, cursor, stmt, params, ctx, many):
        if stmt.startswith("INSERT INTO exam_parsing_operations"):
            raise OperationalError("synthetic", {}, Exception("audit failed"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(OperationalError):
            await c.backend.complete_task_with_artifact(
                c.factory, artifact=artifact, lease_token=claim.lease_token
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.PA)) == 0
        assert await db.scalar(select(func.count()).select_from(service.CA)) == 0
        assert await db.scalar(select(service.J.c.status)) == "running"


@pytest.mark.anyio
async def test_revoked_replay_and_scan_only_legacy_admin_denied(parse_context):
    c = parse_context
    rid = uuid4()
    r = await start(c, rid)
    assert r.status_code == 202
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.parse_grant["grant_id"])
        )
    replay = await start(c, rid)
    assert replay.status_code == 404, replay.text
    assert r.json()["job_id"] not in replay.text
    c.client.headers.update(c.headers("reader", role="admin", is_admin=True))
    for response in [
        await start(c),
        await c.client.get(f"/api/v1/exam-parsing-jobs/{r.json()['job_id']}"),
    ]:
        assert response.status_code == 404, response.text


@pytest.mark.anyio
async def test_cross_session_job_and_canonical_result_are_not_readable(parse_context):
    from huipi_cloud.modules.exam_parsing.permissions import PARSE_READER_ROLE

    c = parse_context
    r = await start(c)
    assert r.status_code == 202
    await consume(c)
    # A valid role on the installation is not used: another real Event scope provides no
    # authority for this session. Use the existing other-user scope from organization setup.
    async with c.factory() as db:
        scopes = (
            (
                await db.execute(
                    select(SecurityScope).where(
                        SecurityScope.kind == "exam_session",
                        SecurityScope.exam_session_id != UUID(c.session["id"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    if not scopes:
        from test_exam_sessions import new_session

        other = await new_session(c, offset=12)
        async with c.factory() as db:
            target = await db.scalar(
                select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(other["id"]))
            )
    else:
        target = scopes[0].id
    async with c.factory() as db:
        await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["reader"].id,
            role_version_id=PARSE_READER_ROLE,
            scope_id=target,
        )
    c.client.headers.update(c.headers("reader"))
    for suffix in ["", "/pages/0"]:
        response = await c.client.get(f"/api/v1/exam-parsing-jobs/{r.json()['job_id']}" + suffix)
        assert response.status_code == 404, response.text
        assert "Synthetic executor" not in response.text


@pytest.mark.anyio
async def test_direct_sql_link_is_immutable_and_job_result_must_match(parse_context):
    c = parse_context
    r = await start(c)
    assert r.status_code == 202
    claim = await c.backend.claim_next_task(c.factory, lease_seconds=180)
    for statement in [
        "UPDATE exam_submission_links SET manifest_sha256=repeat('0',64)",
        "DELETE FROM exam_submission_links",
        "UPDATE exam_parsing_jobs SET answer_script_version_id=gen_random_uuid()",
    ]:
        with pytest.raises(DBAPIError):
            async with c.factory() as db, db.begin():
                await db.execute(text(statement))
    async with c.factory() as db:
        assert await db.scalar(select(service.J.c.status)) == "running"
        assert await db.scalar(select(func.count()).select_from(service.K)) == 1
    # A different token cannot extend or finish the task.
    assert (
        await c.backend.heartbeat(
            c.factory, task_id=claim.task.task_id, lease_token=uuid4(), lease_seconds=180
        )
        is None
    )


@pytest.mark.anyio
async def test_two_exam_workers_cannot_claim_same_job(parse_context):
    c = parse_context
    assert (await start(c)).status_code == 202
    other = ExamBackend(c.storage, c.config)
    claims = await asyncio.gather(
        c.backend.claim_next_task(c.factory, lease_seconds=180),
        other.claim_next_task(c.factory, lease_seconds=180),
    )
    assert sum(item is not None for item in claims) == 1
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.K)) == 1
        assert await db.scalar(select(service.J.c.attempt_count)) == 1


@pytest.mark.anyio
async def test_materialization_audit_failure_does_not_leave_partial_submission(parse_context):
    c = parse_context
    assert (await start(c)).status_code == 202
    engine = c.factory.kw["bind"].sync_engine

    def fail(conn, cursor, stmt, params, ctx, many):
        if stmt.startswith("INSERT INTO exam_parsing_operations") and "materialized" in str(params):
            raise OperationalError("synthetic", {}, Exception("audit failed"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(OperationalError):
            await c.backend.claim_next_task(c.factory, lease_seconds=180)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.K)) == 0
        assert await db.scalar(text("SELECT count(*) FROM submissions")) == 0
    # Claim remains leased and recoverable; no cleanup of an object whose commit is unknown.


@pytest.mark.anyio
async def test_changed_source_bytes_after_materialization_prevent_execution(parse_context):
    c = parse_context
    assert (await start(c)).status_code == 202
    claim = await c.backend.claim_next_task(c.factory, lease_seconds=180)
    from huipi_cloud.modules.scans.models import ScanPage

    async with c.factory() as db:
        original = (
            (await db.execute(select(ScanPage.__table__).where(ScanPage.id == UUID(c.page_ids[0]))))
            .mappings()
            .one()
        )
    remote = await c.storage.download(original["object_key"])
    body = b"".join([b async for b in remote.chunks()])
    await c.storage.upload_fileobj(BytesIO(b"corrupted"), original["object_key"], "image/jpeg")
    try:
        await c.worker._execute_claim(claim)
        assert not c.parser.entered.is_set()
        async with c.factory() as db:
            assert await db.scalar(select(func.count()).select_from(service.PA)) == 0
            assert await db.scalar(select(service.J.c.status)) != "succeeded"
    finally:
        await c.storage.upload_fileobj(BytesIO(body), original["object_key"], "image/jpeg")


@pytest.mark.anyio
async def test_result_reader_cannot_start_and_does_not_read_answer_keys(parse_context):
    from huipi_cloud.modules.exam_parsing.permissions import PARSE_READER_ROLE

    c = parse_context
    r = await start(c)
    assert r.status_code == 202
    await consume(c)
    async with c.factory() as db:
        target = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["reader"].id,
            role_version_id=PARSE_READER_ROLE,
            scope_id=target,
        )
    c.client.headers.update(c.headers("reader", role="admin", is_admin=True))
    assert (await start(c)).status_code == 404
    status = await c.client.get(f"/api/v1/exam-parsing-jobs/{r.json()['job_id']}")
    assert status.status_code == 200 and status.json()["status"] == "succeeded", status.text
    page = await c.client.get(f"/api/v1/exam-parsing-jobs/{r.json()['job_id']}/pages/0")
    assert page.status_code == 200, page.text
    assert all(k not in status.text + page.text for k in ["object_key", "answer_key", "rubric"])


@pytest.mark.anyio
async def test_new_job_fact_refuses_destructive_downgrade(parse_context):
    import sys

    c = parse_context
    r = await start(c)
    assert r.status_code == 202
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "downgrade",
        "b14a30f1d902",
        env=dict(os.environ, DATABASE_URL=os.environ["TEST_DATABASE_URL"]),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), 20)
    assert process.returncode != 0 and b"exam_parse_downgrade_requires_archived_data" in output
    async with c.factory() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "b14c52b3f014"
        assert await db.scalar(select(service.J.c.id)) == UUID(r.json()["job_id"])


@pytest.mark.anyio
async def test_corrupt_canonical_object_returns_safe_error(parse_context):
    c = parse_context
    r = await start(c)
    assert r.status_code == 202
    await consume(c)
    async with c.factory() as db:
        value = (await db.execute(select(service.CA))).mappings().one()
    remote = await c.storage.download(value["canonical_object_key"])
    original = b"".join([b async for b in remote.chunks()])
    await c.storage.upload_fileobj(
        BytesIO(b"corrupt"), value["canonical_object_key"], "application/json"
    )
    try:
        response = await c.client.get(f"/api/v1/exam-parsing-jobs/{r.json()['job_id']}/pages/0")
        assert response.status_code == 503, response.text
        assert response.json()["detail"] == "exam_parse_result_unavailable"
        assert value["canonical_object_key"] not in response.text
    finally:
        await c.storage.upload_fileobj(
            BytesIO(original), value["canonical_object_key"], "application/json"
        )


@pytest.mark.anyio
async def test_new_confirmed_version_has_new_link_and_old_result_is_historical(parse_context):
    c = parse_context
    first = await start(c)
    assert first.status_code == 202
    await consume(c)
    old_version = c.answer["version_id"]
    body = confirmation(c, c.answer)
    body["page_ids"] = c.page_ids[:2]
    revised = await c.client.post(
        f"/api/v1/answer-scripts/{c.answer['script_id']}/versions", json=body
    )
    assert revised.status_code == 200, revised.text
    c.answer = revised.json()
    second = await start(c)
    assert second.status_code == 202
    assert second.json() != first.json()
    await consume(c)
    history = await c.client.get(f"/api/v1/exam-parsing-jobs/{first.json()['job_id']}")
    assert (
        history.json()["status"] == "invalidated"
        and history.json()["canonical_artifact_id"] is None
    )
    assert history.json()["answer_script_version_id"] == old_version
    assert (
        await c.client.get(f"/api/v1/exam-parsing-jobs/{first.json()['job_id']}/pages/0")
    ).status_code == 409
    current = await c.client.get(f"/api/v1/answer-scripts/{c.answer['script_id']}/parsing")
    assert current.json()["status"] == "succeeded"
    assert current.json()["answer_script_version_id"] == c.answer["version_id"]
    async with c.factory() as db:
        links = (await db.execute(select(service.K))).mappings().all()
        assert len(links) == 2 and len({r["submission_id"] for r in links}) == 2
        assert await db.scalar(select(func.count()).select_from(service.PA)) == 2


@pytest.mark.anyio
async def test_database_rejects_partial_lease_and_result_shapes(parse_context):
    c = parse_context
    assert (await start(c)).status_code == 202
    for statement in [
        "UPDATE exam_parsing_jobs SET lease_token=gen_random_uuid()",
        "UPDATE exam_parsing_jobs SET parsed_artifact_id=gen_random_uuid()",
    ]:
        with pytest.raises(DBAPIError) as caught:
            async with c.factory() as db, db.begin():
                await db.execute(text(statement))
        assert caught.value.orig.sqlstate == "23514"
    async with c.factory() as db:
        job = (await db.execute(select(service.J))).mappings().one()
        assert job["status"] == "pending" and job["lease_token"] is None
        assert job["parsed_artifact_id"] is None and job["canonical_artifact_id"] is None


@pytest.mark.anyio
async def test_shared_job_request_audit_uses_actual_requester_and_replays(parse_context):
    c = parse_context
    first = await start(c)
    assert first.status_code == 202
    async with c.factory() as db:
        target = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        grant = await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["b"].id,
            role_version_id=PARSE_OPERATOR_ROLE,
            scope_id=target,
        )
    c.client.headers.update(c.headers("b"))
    rid = uuid4()
    second = await start(c, rid)
    repeat = await start(c, rid)
    assert second.status_code == repeat.status_code == 202, (second.text, repeat.text)
    assert first.json() == second.json() == repeat.json()
    async with c.factory() as db:
        op = (
            (await db.execute(select(service.OPS).where(service.OPS.c.request_id == rid)))
            .mappings()
            .one()
        )
        assert op["actor_user_id"] == c.users["b"].id
        assert await db.scalar(select(func.count()).select_from(service.J)) == 1
        assert await db.scalar(select(service.J.c.requested_by_user_id)) == c.users["a"].id
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(grant["grant_id"])
        )
    assert (await start(c, rid)).status_code == 404
