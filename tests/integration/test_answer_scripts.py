"""Real signed JWT, PostgreSQL, private S3 and immutable source bytes."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_scan_intake import auth_api, exam, papers, roster, scan_context, upload  # noqa: F401

from huipi_cloud.modules.answer_scripts import service
from huipi_cloud.modules.answer_scripts.permissions import SCRIPT_OPERATOR_ROLE
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityScope


@pytest.fixture
async def script_context(scan_context):  # noqa: F811
    c = scan_context
    async with c.factory() as db:
        await service.enable_operator(
            db, c.users["admin"].id, c.users["admin"].id, uuid4(), "synthetic script deployment"
        )
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        c.script_grant = await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=SCRIPT_OPERATOR_ROLE,
            scope_id=scope,
        )
    result = await upload(c)
    assert result.status_code == 201, result.text
    c.scan_file = result.json()
    pages = await c.client.get(f"/api/v1/scan-files/{c.scan_file['id']}/pages")
    c.page_ids = [p["id"] for p in pages.json()["items"]]
    try:
        yield c
    finally:
        async with c.factory() as db, db.begin():
            await db.execute(text("TRUNCATE answer_script_operations,answer_scripts CASCADE"))


def base(c):
    return f"/api/v1/exam-sessions/{c.session['id']}"


async def draft(c, ids=None, rid=None):
    return await c.client.post(
        base(c) + "/answer-scripts",
        json=dict(
            request_id=str(rid or uuid4()),
            page_ids=ids or c.page_ids[:2],
            completeness="complete",
            reason="人工核对页组",
        ),
    )


async def candidates(c):
    r = await c.client.get(base(c) + "/identity-candidates")
    assert r.status_code == 200, r.text
    return r.json()


def confirmation(c, d, choices=None, index=0, **changes):
    r = choices or c.candidates
    return dict(
        request_id=str(uuid4()),
        expected_revision=d["revision"],
        action="confirm",
        page_ids=c.page_ids[:2],
        completeness="complete",
        reason="核对原卷与名单",
        roster_revision_id=r["roster_revision_id"],
        roster_entry_id=r["items"][index]["roster_entry_id"],
        session_paper_id=r["papers"][0]["session_paper_id"],
        **changes,
    )


@pytest.mark.anyio
async def test_group_confirm_manifest_history_and_original_unchanged(script_context):
    c = script_context
    c.candidates = await candidates(c)
    assert len(c.candidates["items"]) == 4
    same = [
        i
        for i in c.candidates["items"]
        if i["student_name"] == c.candidates["items"][0]["student_name"]
    ]
    assert len({i["participation_id"] for i in same}) == len(same)
    r = await draft(c)
    assert r.status_code == 201, r.text
    d = r.json()
    aid = d["script_id"]
    body = confirmation(c, d)
    body["page_ids"] = list(reversed(c.page_ids[:2]))
    r = await c.client.post(f"/api/v1/answer-scripts/{aid}/versions", json=body)
    assert r.status_code == 200, r.text
    view = await c.client.get(f"/api/v1/answer-scripts/{aid}")
    assert view.json()["readiness"] == "identity_confirmed"
    assert [p["scan_page_id"] for p in view.json()["current"]["pages"]] == body["page_ids"]
    manifest = await c.client.get(f"/api/v1/answer-scripts/{aid}/manifest")
    assert manifest.status_code == 200, manifest.text
    assert manifest.json()["bridge_implemented"] is False
    assert not any(x in manifest.text for x in ("object_key", "bucket", "access_key"))
    history = await c.client.get(f"/api/v1/answer-scripts/{aid}/versions")
    assert [v["status"] for v in history.json()["items"]] == ["confirmed", "draft"]
    assert history.json()["items"][0]["previous_version_id"] == d["version_id"]
    free = await c.client.get(base(c) + "/script-pages?unclaimed=true")
    assert free.json()["total"] == 2
    async with c.factory() as db:
        assert (
            await db.scalar(
                text("SELECT count(*) FROM scan_pages WHERE identity_status='unconfirmed'")
            )
            == 4
        )
        assert await db.scalar(text("SELECT count(*) FROM submissions")) == 0


@pytest.mark.anyio
async def test_same_page_competition_only_one_current_owner(script_context):
    c = script_context
    a, b = await asyncio.gather(draft(c), draft(c))
    assert sorted([a.status_code, b.status_code]) == [201, 409], (a.text, b.text)
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.A)) == 1
        assert await db.scalar(select(func.count()).select_from(service.C)) == 2


@pytest.mark.anyio
async def test_direct_sql_immutability_and_complete_allocation(script_context):
    c = script_context
    d = (await draft(c)).json()
    for sql, params in [
        (
            "UPDATE answer_script_versions SET reason='篡改' WHERE id=:id",
            dict(id=UUID(d["version_id"])),
        ),
        ("DELETE FROM answer_script_pages WHERE version_id=:id", dict(id=UUID(d["version_id"]))),
        (
            "DELETE FROM current_page_allocations WHERE answer_script_id=:id",
            dict(id=UUID(d["script_id"])),
        ),
        (
            "UPDATE answer_scripts SET current_participation_id=:p WHERE id=:id",
            dict(id=UUID(d["script_id"]), p=uuid4()),
        ),
    ]:
        with pytest.raises(DBAPIError):
            async with c.factory() as db, db.begin():
                await db.execute(text(sql), params)
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.C)) == 2


@pytest.mark.anyio
async def test_idempotency_body_conflict_and_revocation(script_context):
    c = script_context
    rid = uuid4()
    a = await draft(c, rid=rid)
    b = await draft(c, rid=rid)
    assert a.json() == b.json()
    conflict = await draft(c, c.page_ids[2:], rid)
    assert conflict.status_code == 409
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.script_grant["grant_id"])
        )
    denied = await draft(c, rid=rid)
    assert denied.status_code == 404, denied.text
    assert a.json()["script_id"] not in denied.text


@pytest.mark.anyio
async def test_stale_roster_reconfirm_and_unknown_variant_not_inferred(script_context):
    from test_exam_rosters import call, entries, freeze, req

    from huipi_cloud.modules.exam_rosters import service as rosters
    from huipi_cloud.modules.exam_rosters.schemas import RosterCorrection

    c = script_context
    c.candidates = await candidates(c)
    d = (await draft(c)).json()
    aid = d["script_id"]
    body = confirmation(c, d)
    assert (
        await c.client.post(f"/api/v1/answer-scripts/{aid}/versions", json=body)
    ).status_code == 200
    changed = await call(
        c,
        rosters.correct_roster,
        RosterCorrection(
            **req(c.frozen_roster).model_dump(),
            reason="合成名单更正",
            entries=entries(c, 4, unknown=True),
        ),
    )
    await freeze(c, changed)
    value = (await c.client.get(f"/api/v1/answer-scripts/{aid}")).json()
    assert value["readiness"] == "roster_changed"
    assert (await c.client.get(f"/api/v1/answer-scripts/{aid}/manifest")).status_code == 409
    body.update(request_id=str(uuid4()), expected_revision=2)
    assert (
        await c.client.post(f"/api/v1/answer-scripts/{aid}/versions", json=body)
    ).status_code == 409
    choices = await candidates(c)
    assert all(i["session_paper_id"] is None for i in choices["items"])
    body.update(
        roster_revision_id=choices["roster_revision_id"],
        roster_entry_id=choices["items"][0]["roster_entry_id"],
        request_id=str(uuid4()),
    )
    body["session_paper_id"] = None
    assert (
        await c.client.post(f"/api/v1/answer-scripts/{aid}/versions", json=body)
    ).status_code == 422
    body["session_paper_id"] = choices["papers"][0]["session_paper_id"]
    assert (
        await c.client.post(f"/api/v1/answer-scripts/{aid}/versions", json=body)
    ).status_code == 200


@pytest.mark.anyio
async def test_cross_file_append_revoke_keeps_original_and_versions(script_context):
    from test_scan_intake import pdf_bytes

    c = script_context
    d = (await draft(c)).json()
    aid = d["script_id"]
    c.batch = (
        await c.client.post(
            base(c) + "/scan-batches", json=dict(request_id=str(uuid4()), title="合成跨批次补页")
        )
    ).json()
    extra = await upload(c, pdf_bytes(1, label="Synthetic supplementary page"))
    extra_pages = (await c.client.get(f"/api/v1/scan-files/{extra.json()['id']}/pages")).json()[
        "items"
    ]
    body = dict(
        request_id=str(uuid4()),
        expected_revision=1,
        action="save_draft",
        page_ids=c.page_ids[:2] + [extra_pages[0]["id"]],
        completeness="complete",
        reason="补扫页按顺序追加",
    )
    r = await c.client.post(f"/api/v1/answer-scripts/{aid}/versions", json=body)
    assert r.status_code == 200, r.text
    h = (await c.client.get(f"/api/v1/answer-scripts/{aid}/versions")).json()["items"]
    assert len(h[0]["pages"]) == 3 and len(h[1]["pages"]) == 2
    body.update(
        request_id=str(uuid4()), expected_revision=2, action="revoke", reason="人工撤销分卷"
    )
    assert (
        await c.client.post(f"/api/v1/answer-scripts/{aid}/versions", json=body)
    ).status_code == 200
    free = (await c.client.get(base(c) + "/script-pages?unclaimed=true")).json()
    assert free["total"] == 5
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.L)) == 8
        assert await db.scalar(text("SELECT count(*) FROM scan_pages")) == 5


@pytest.mark.anyio
async def test_storage_failure_prevents_confirmed_decision(script_context, monkeypatch):
    from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError

    c = script_context
    c.candidates = await candidates(c)
    d = (await draft(c)).json()

    async def unavailable(*args):
        raise StorageUnavailableError("synthetic outage")

    monkeypatch.setattr(c.storage, "download", unavailable)
    r = await c.client.post(
        f"/api/v1/answer-scripts/{d['script_id']}/versions", json=confirmation(c, d)
    )
    assert r.status_code == 503, r.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.V)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(service.OPS)
                .where(service.OPS.c.action == "confirm")
            )
            == 0
        )


@pytest.mark.anyio
async def test_confirm_rechecks_grant_after_storage_io(script_context, monkeypatch):
    c = script_context
    c.candidates = await candidates(c)
    d = (await draft(c)).json()
    entered, release = asyncio.Event(), asyncio.Event()
    real = service.verify_sources

    async def held(*args):
        await real(*args)
        entered.set()
        await release.wait()

    monkeypatch.setattr(service, "verify_sources", held)
    task = asyncio.create_task(
        c.client.post(f"/api/v1/answer-scripts/{d['script_id']}/versions", json=confirmation(c, d))
    )
    await asyncio.wait_for(entered.wait(), 10)
    try:
        async with c.factory() as db:
            await security.revoke_grant(
                db, c.principals["admin"], uuid4(), grant_id=UUID(c.script_grant["grant_id"])
            )
    finally:
        release.set()
    r = await task
    assert r.status_code == 404, r.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.V)) == 1


@pytest.mark.anyio
async def test_audit_failure_rolls_back_entire_group(script_context):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    c = script_context
    engine = c.factory.kw["bind"].sync_engine

    def failure(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO answer_script_operations"):
            raise OperationalError("synthetic", {}, Exception("audit unavailable"))

    event.listen(engine, "before_cursor_execute", failure)
    try:
        r = await draft(c)
    finally:
        event.remove(engine, "before_cursor_execute", failure)
    assert r.status_code == 503, r.text
    async with c.factory() as db:
        for table in (service.A, service.V, service.L, service.C):
            assert await db.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.anyio
async def test_commit_unknown_replay_returns_exact_receipt(script_context):
    from sqlalchemy.exc import OperationalError

    from huipi_cloud.infrastructure.database.session import get_db_session
    from huipi_cloud.main import app

    c = script_context
    rid = uuid4()

    async def lost_session():
        async with c.factory() as db:
            original = db.commit
            commits = 0

            async def commit():
                nonlocal commits
                commits += 1
                await original()
                if commits == 2:
                    raise OperationalError("synthetic", {}, Exception("lost ACK"))

            db.commit = commit
            yield db

    previous = app.dependency_overrides[get_db_session]
    app.dependency_overrides[get_db_session] = lost_session
    try:
        r = await draft(c, rid=rid)
    finally:
        app.dependency_overrides[get_db_session] = previous
    assert r.status_code == 503, r.text
    again = await draft(c, rid=rid)
    assert again.status_code == 201, again.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.A)) == 1
        assert (
            await db.scalar(select(func.count()).select_from(service.OPS)) == 2
        )  # deployment + create


@pytest.mark.anyio
async def test_two_same_named_students_and_participation_unique(script_context):
    c = script_context
    choices = await candidates(c)
    names = {i["student_name"] for i in choices["items"]}
    twins = next(
        [i for i in choices["items"] if i["student_name"] == n]
        for n in names
        if sum(i["student_name"] == n for i in choices["items"]) == 2
    )
    assert twins[0]["student_code"] != twins[1]["student_code"]
    first = (await draft(c)).json()
    second = (await draft(c, c.page_ids[2:])).json()
    for index, (d, twin) in enumerate(zip([first, second], twins, strict=True)):
        body = confirmation(c, d, choices=choices)
        body["roster_entry_id"] = twin["roster_entry_id"]
        body["page_ids"] = c.page_ids[index * 2 : index * 2 + 2]
        r = await c.client.post(f"/api/v1/answer-scripts/{d['script_id']}/versions", json=body)
        assert r.status_code == 200, r.text
    body = confirmation(c, dict(revision=2), choices=choices)
    body["roster_entry_id"] = twins[0]["roster_entry_id"]
    body["page_ids"] = c.page_ids[2:]
    r = await c.client.post(f"/api/v1/answer-scripts/{second['script_id']}/versions", json=body)
    assert r.status_code in (
        409,
        503,
    )  # Deferred/unique database enforcement never a confirmed success.
    async with c.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(service.A)
                .where(service.A.c.current_participation_id.is_not(None))
            )
            == 2
        )


@pytest.mark.anyio
async def test_cross_session_identity_and_page_rejected(script_context):
    from test_exam_sessions import bind_request, call, new_session, state_request
    from test_scan_intake import pdf_bytes

    from huipi_cloud.modules.exams import service as exams

    c = script_context
    other = await new_session(c, offset=5)
    denied = await c.client.get(f"/api/v1/exam-sessions/{other['id']}/identity-candidates")
    assert denied.status_code == 404 and "student_name" not in denied.text
    bound = await call(c, exams.bind_paper, bind_request(c, other))
    other = await call(c, exams.change_session_state, state_request(bound["session"], "ready"))
    choices = await candidates(c)
    d = (await draft(c)).json()
    body = confirmation(c, d, choices=choices)
    body["session_paper_id"] = bound["binding"]["id"]
    assert (
        await c.client.post(f"/api/v1/answer-scripts/{d['script_id']}/versions", json=body)
    ).status_code == 404
    old_batch = c.batch
    c.client.headers.update(c.headers("admin"))
    c.batch = (
        await c.client.post(
            f"/api/v1/exam-sessions/{other['id']}/scan-batches",
            json=dict(request_id=str(uuid4()), title="other session"),
        )
    ).json()
    uploaded = await upload(c, pdf_bytes(1))
    assert uploaded.status_code == 201, uploaded.text
    foreign = (await c.client.get(f"/api/v1/scan-files/{uploaded.json()['id']}/pages")).json()[
        "items"
    ][0]["id"]
    c.client.headers.update(c.headers("a"))
    c.batch = old_batch
    assert (await draft(c, [foreign])).status_code == 404
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.A)) == 1


@pytest.mark.anyio
async def test_concurrent_version_changes_preserve_single_successor(script_context):
    c = script_context
    d = (await draft(c)).json()
    aid = d["script_id"]

    def body():
        return dict(
            request_id=str(uuid4()),
            expected_revision=1,
            action="save_draft",
            page_ids=list(reversed(c.page_ids[:2])),
            completeness="incomplete",
            reason="并发调整页序",
        )

    a, b = await asyncio.gather(
        *(c.client.post(f"/api/v1/answer-scripts/{aid}/versions", json=body()) for _ in range(2))
    )
    assert sorted([a.status_code, b.status_code]) == [200, 409]
    async with c.factory() as db:
        versions = (
            (await db.execute(select(service.V).order_by(service.V.c.version_no))).mappings().all()
        )
        assert len(versions) == 2 and versions[1]["previous_version_id"] == UUID(d["version_id"])
        assert await db.scalar(select(func.count()).select_from(service.C)) == 2


@pytest.mark.anyio
async def test_incomplete_identity_does_not_create_ready_manifest(script_context):
    c = script_context
    c.candidates = await candidates(c)
    d = (await draft(c)).json()
    body = confirmation(c, d)
    body["completeness"] = "incomplete"
    assert (
        await c.client.post(f"/api/v1/answer-scripts/{d['script_id']}/versions", json=body)
    ).status_code == 200
    result = await c.client.get(f"/api/v1/answer-scripts/{d['script_id']}")
    assert result.json()["readiness"] == "incomplete"
    assert (
        await c.client.get(f"/api/v1/answer-scripts/{d['script_id']}/manifest")
    ).status_code == 409
    async with c.factory() as db:
        assert await db.scalar(text("SELECT count(*) FROM parsing_tasks")) == 0


@pytest.mark.anyio
async def test_scan_only_and_legacy_admin_cannot_read_identity_or_scripts(script_context):
    c = script_context
    # b is an ordinary teacher with no exam scope; JWT role and old admin remain irrelevant.
    async with c.factory() as db, db.begin():
        await db.execute(
            text("UPDATE users SET is_admin=true WHERE id=:id"), dict(id=c.users["b"].id)
        )
    for name in ["b", "reader"]:
        c.client.headers.update(c.headers(name))
        r = await c.client.get(base(c) + "/identity-candidates")
        assert r.status_code == 404, r.text
        assert "合成同名" not in r.text
    c.client.headers.update(c.headers("a"))
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.script_grant["grant_id"])
        )
    # Existing scan operator has scan access but no student-list or script action.
    assert (await c.client.get(base(c) + "/script-pages")).status_code == 404
    assert (await c.client.get(base(c) + "/identity-candidates")).status_code == 404


@pytest.mark.anyio
async def test_split_grants_do_not_compose_confirmation_authority(script_context):
    from huipi_cloud.modules.answer_scripts.permissions import (
        SCRIPT_EDITOR_ROLE,
        SCRIPT_VERIFIER_ROLE,
    )

    c = script_context
    choices = await candidates(c)
    d = (await draft(c)).json()
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.script_grant["grant_id"])
        )
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    for rid in [SCRIPT_EDITOR_ROLE, SCRIPT_VERIFIER_ROLE]:
        async with c.factory() as db:
            await security.grant_role(
                db,
                c.principals["admin"],
                uuid4(),
                user_id=c.users["a"].id,
                role_version_id=rid,
                scope_id=scope,
            )
    r = await c.client.post(
        f"/api/v1/answer-scripts/{d['script_id']}/versions",
        json=confirmation(c, d, choices=choices),
    )
    assert r.status_code == 404, r.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.V)) == 1


@pytest.mark.anyio
async def test_source_tampering_and_retirement_fail_readiness(script_context):
    from test_paper_versions import write as paper_write

    from huipi_cloud.modules.papers import service as paper_service

    c = script_context
    c.candidates = await candidates(c)
    d = (await draft(c)).json()
    r = await c.client.post(
        f"/api/v1/answer-scripts/{d['script_id']}/versions", json=confirmation(c, d)
    )
    assert r.status_code == 200, r.text
    # Retire through the real service; historical confirmation remains but cannot be bridged.
    async with c.factory() as db:
        version = await service.exams.row(db, service.PV, UUID(c.paper["paper_version_id"]))
    async with c.factory() as db:
        await paper_service.retire_paper(
            db,
            c.principals["admin"],
            paper_write(
                {
                    "paper_version_id": version["id"],
                    "revision": version["revision"],
                    "content_sha256": version["content_sha256"],
                    "mapping_digest": version["mapping_digest"],
                }
            ),
        )
    result = (await c.client.get(f"/api/v1/answer-scripts/{d['script_id']}")).json()
    assert result["readiness"] == "paper_unavailable" and result["current"]["status"] == "confirmed"
    assert (
        await c.client.get(f"/api/v1/answer-scripts/{d['script_id']}/manifest")
    ).status_code == 409


@pytest.mark.anyio
async def test_source_sha_mismatch_cannot_confirm(script_context):
    c = script_context
    c.candidates = await candidates(c)
    d = (await draft(c)).json()
    before = await c.client.get(f"/api/v1/scan-pages/{c.page_ids[0]}/preview")
    assert before.status_code == 200
    async with c.factory() as db:
        page = await service.exams.row(db, service.P, UUID(c.page_ids[0]))
    client = c.storage.storage._client
    await asyncio.to_thread(
        client.put_object,
        Bucket=c.storage.bucket,
        Key=page["object_key"],
        Body=b"broken synthetic preview",
    )
    try:
        r = await c.client.post(
            f"/api/v1/answer-scripts/{d['script_id']}/versions", json=confirmation(c, d)
        )
        assert r.status_code == 503, r.text
        async with c.factory() as db:
            assert await db.scalar(select(func.count()).select_from(service.V)) == 1
    finally:
        await asyncio.to_thread(
            client.put_object, Bucket=c.storage.bucket, Key=page["object_key"], Body=before.content
        )


@pytest.mark.anyio
async def test_downgrade_refuses_live_scripts_without_losing_history(script_context):
    import os
    import sys
    from pathlib import Path

    c = script_context
    d = (await draft(c)).json()
    environment = dict(os.environ, DATABASE_URL=os.environ["TEST_DATABASE_URL"])
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "downgrade",
        "b13a21f0c901",
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(proc.communicate(), 20)
    assert proc.returncode != 0 and b"script_downgrade_requires_archived_new_data" in output
    async with c.factory() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "b14a30f1d902"
        assert await db.scalar(
            select(service.A.c.current_version_id).where(service.A.c.id == UUID(d["script_id"]))
        ) == UUID(d["version_id"])


@pytest.mark.anyio
async def test_concurrent_confirmation_cannot_steal_current_pages(script_context):
    c = script_context
    choices = await candidates(c)
    drafts = [(await draft(c)).json(), (await draft(c, c.page_ids[2:])).json()]
    bodies = [confirmation(c, d, choices=choices, index=i) for i, d in enumerate(drafts)]
    responses = await asyncio.gather(
        *(
            c.client.post(f"/api/v1/answer-scripts/{d['script_id']}/versions", json=b)
            for d, b in zip(drafts, bodies, strict=True)
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    async with c.factory() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(service.V).where(service.V.c.status == "confirmed")
            )
            == 1
        )
        assert await db.scalar(select(func.count()).select_from(service.C)) == 4


@pytest.mark.anyio
async def test_ab_known_variant_rejects_override_then_explicit_unknown_can_confirm_b(
    script_context,
):
    from test_exam_rosters import create, entries, freeze, req
    from test_exam_sessions import bind_request, call, new_session, state_request
    from test_paper_versions import lock, write
    from test_scan_intake import pdf_bytes

    from huipi_cloud.modules.exam_rosters import service as rosters
    from huipi_cloud.modules.exam_rosters.schemas import RosterCorrection
    from huipi_cloud.modules.exams import service as exams
    from huipi_cloud.modules.papers import service as paper_service

    c = script_context
    async with c.factory() as db:
        successor = await paper_service.fork_paper(db, c.principals["admin"], write(c.paper))
    second_paper = await lock(c, successor)
    c.session = await new_session(c, offset=9, policy="ab")
    a = await call(c, exams.bind_paper, bind_request(c, c.session, variant_code="A"))
    c.session = a["session"]
    original_paper = c.paper
    c.paper = second_paper
    b = await call(c, exams.bind_paper, bind_request(c, c.session, variant_code="B"))
    c.paper = original_paper
    c.binding = a["binding"]
    c.session = b["session"]
    roster_draft = await create(c, entries(c, 4))
    c.session = await call(c, exams.change_session_state, state_request(c.session, "ready"))
    frozen = await freeze(c, roster_draft)
    c.client.headers.update(c.headers("admin"))
    c.batch = (
        await c.client.post(
            base(c) + "/scan-batches", json=dict(request_id=str(uuid4()), title="合成AB分卷")
        )
    ).json()
    r = await upload(c, pdf_bytes(2, label="Synthetic AB paper"))
    assert r.status_code == 201, r.text
    c.page_ids = [
        p["id"]
        for p in (await c.client.get(f"/api/v1/scan-files/{r.json()['id']}/pages")).json()["items"]
    ]
    choices = await candidates(c)
    assert [p["variant_code"] for p in choices["papers"]] == ["A", "B"]
    d = (await draft(c)).json()
    body = confirmation(c, d, choices=choices)
    body["session_paper_id"] = b["binding"]["id"]
    assert (
        await c.client.post(f"/api/v1/answer-scripts/{d['script_id']}/versions", json=body)
    ).status_code == 409
    body["session_paper_id"] = a["binding"]["id"]
    body["request_id"] = str(uuid4())
    assert (
        await c.client.post(f"/api/v1/answer-scripts/{d['script_id']}/versions", json=body)
    ).status_code == 200
    correction = await call(
        c,
        rosters.correct_roster,
        RosterCorrection(
            **req(frozen).model_dump(),
            reason="按原卷核查未知卷别",
            entries=entries(c, 4, unknown=True),
        ),
    )
    await freeze(c, correction)
    choices = await candidates(c)
    body = confirmation(c, dict(revision=2), choices=choices)
    body["session_paper_id"] = b["binding"]["id"]
    r = await c.client.post(f"/api/v1/answer-scripts/{d['script_id']}/versions", json=body)
    assert r.status_code == 200, r.text
    history = (await c.client.get(f"/api/v1/answer-scripts/{d['script_id']}/versions")).json()[
        "items"
    ]
    assert (
        history[0]["session_paper_id"] == b["binding"]["id"]
        and history[1]["session_paper_id"] == a["binding"]["id"]
    )


@pytest.mark.anyio
async def test_confirm_audit_failure_preserves_previous_draft(script_context):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    c = script_context
    choices = await candidates(c)
    d = (await draft(c)).json()
    engine = c.factory.kw["bind"].sync_engine

    def failure(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO answer_script_operations"):
            raise OperationalError("synthetic", {}, Exception("audit failure"))

    event.listen(engine, "before_cursor_execute", failure)
    try:
        r = await c.client.post(
            f"/api/v1/answer-scripts/{d['script_id']}/versions",
            json=confirmation(c, d, choices=choices),
        )
    finally:
        event.remove(engine, "before_cursor_execute", failure)
    assert r.status_code == 503
    view = (await c.client.get(f"/api/v1/answer-scripts/{d['script_id']}")).json()
    assert view["revision"] == 1 and view["current"]["status"] == "draft"
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.V)) == 1
        assert await db.scalar(select(func.count()).select_from(service.C)) == 2


@pytest.mark.anyio
async def test_roster_changes_during_confirmation_io_block_commit(script_context, monkeypatch):
    from test_exam_rosters import call, entries, freeze, req

    from huipi_cloud.modules.exam_rosters import service as rosters
    from huipi_cloud.modules.exam_rosters.schemas import RosterCorrection

    c = script_context
    choices = await candidates(c)
    d = (await draft(c)).json()
    entered, release = asyncio.Event(), asyncio.Event()
    real = service.verify_sources

    async def held(*args):
        await real(*args)
        entered.set()
        await release.wait()

    monkeypatch.setattr(service, "verify_sources", held)
    task = asyncio.create_task(
        c.client.post(
            f"/api/v1/answer-scripts/{d['script_id']}/versions",
            json=confirmation(c, d, choices=choices),
        )
    )
    await asyncio.wait_for(entered.wait(), 10)
    try:
        corrected = await call(
            c,
            rosters.correct_roster,
            RosterCorrection(
                **req(c.frozen_roster).model_dump(),
                reason="核验期间名单更正",
                entries=entries(c, 4),
            ),
        )
        await freeze(c, corrected)
    finally:
        release.set()
    r = await task
    assert r.status_code == 409, r.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.V)) == 1


@pytest.mark.anyio
async def test_source_file_bytes_and_scan_identity_remain_unchanged(script_context):
    import hashlib

    c = script_context
    choices = await candidates(c)
    d = (await draft(c)).json()
    original = await c.client.get(f"/api/v1/scan-files/{c.scan_file['id']}/original")
    async with c.factory() as db:
        before = [
            tuple(r) for r in (await db.execute(select(service.P).order_by(service.P.c.id))).all()
        ]
    assert (
        await c.client.post(
            f"/api/v1/answer-scripts/{d['script_id']}/versions",
            json=confirmation(c, d, choices=choices),
        )
    ).status_code == 200
    after = await c.client.get(f"/api/v1/scan-files/{c.scan_file['id']}/original")
    assert after.content == original.content
    assert hashlib.sha256(after.content).hexdigest() == c.scan_file["sha256"]
    async with c.factory() as db:
        assert [
            tuple(r) for r in (await db.execute(select(service.P).order_by(service.P.c.id))).all()
        ] == before
        assert await db.scalar(text("SELECT count(*) FROM submissions")) == 0
        assert await db.scalar(text("SELECT count(*) FROM parsing_tasks")) == 0


@pytest.mark.anyio
async def test_script_reader_without_roster_permission_has_redacted_history(script_context):
    from huipi_cloud.modules.answer_scripts.permissions import SCRIPT_READER_ROLE

    c = script_context
    choices = await candidates(c)
    d = (await draft(c)).json()
    assert (
        await c.client.post(
            f"/api/v1/answer-scripts/{d['script_id']}/versions",
            json=confirmation(c, d, choices=choices),
        )
    ).status_code == 200
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["reader"].id,
            role_version_id=SCRIPT_READER_ROLE,
            scope_id=scope,
        )
    c.client.headers.update(c.headers("reader", role="admin", is_admin=True))
    r = await c.client.get(f"/api/v1/answer-scripts/{d['script_id']}/versions")
    assert r.status_code == 200, r.text
    for version in r.json()["items"]:
        assert version["identity"] is None
        assert all(
            version[k] is None
            for k in [
                "roster_revision_id",
                "roster_entry_id",
                "participation_id",
                "session_paper_id",
            ]
        )
    assert "合成同名" not in r.text
    assert (await c.client.get(base(c) + "/identity-candidates")).status_code == 404
    assert (
        await c.client.post(
            f"/api/v1/answer-scripts/{d['script_id']}/versions",
            json=confirmation(c, dict(revision=2), choices=choices),
        )
    ).status_code == 404
