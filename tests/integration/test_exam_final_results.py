"""Real PostgreSQL/S3: authorized attendance, immutable publication and correction."""
# ruff: noqa: F401, F811

from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_exam_results import (
    allow,
    auth_api,
    base,
    consume,
    detail,
    exam,
    marking,
    papers,
    parse_context,
    read,
    results,
    review,
    roster,
    scan_context,
    score,
    script_context,
    start,
    target,
)

from huipi_cloud.modules.exam_results import access
from huipi_cloud.modules.exam_results import final_service as final
from huipi_cloud.modules.exam_results.permissions import RESULT_MANAGER_ROLE
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityScope


@pytest.fixture
async def final_context(results):
    c = results
    async with c.factory() as db:
        await final.enable_operator(
            db, c.users["admin"].id, c.users["admin"].id, uuid4(), "合成部署审批"
        )
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        grant = await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=RESULT_MANAGER_ROLE,
            scope_id=scope,
        )
    for cls in c.classes:
        async with c.factory() as db:
            await access.grant_class(
                db,
                c.principals["admin"],
                uuid4(),
                grant_id=UUID(grant["grant_id"]),
                session_id=UUID(c.session["id"]),
                class_id=cls.id,
                export_allowed=True,
            )
    c.final_grant = grant
    yield c


def root(c):
    return f"/api/v1/exam-sessions/{c.session['id']}/final-results"


async def set_attendance(c):
    response = await c.client.get(root(c) + "/attendance")
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        response = await c.client.post(
            root(c) + "/attendance",
            json=dict(
                request_id=str(uuid4()),
                participation_id=item["participation_id"],
                expected_revision=item["revision"],
                status="present"
                if item["participation_id"] == c.results_participation
                else "absent",
                reason="合成现场人工核验",
            ),
        )
        assert response.status_code == 200, response.text


async def prepare(c, previous=None):
    return await c.client.post(
        root(c) + "/versions",
        json=dict(
            request_id=str(uuid4()),
            previous_version_id=previous,
            reason="合成成绩核验",
        ),
    )


async def step(c, version, action, revision):
    return await c.client.post(
        root(c) + f"/versions/{version}/transition",
        json=dict(
            request_id=str(uuid4()),
            expected_revision=revision,
            action=action,
            reason="合成考务审核",
        ),
    )


@pytest.mark.anyio
async def test_complete_publish_correct_and_export(final_context, postgres_engine):
    c = final_context
    await set_attendance(c)
    assert (await prepare(c)).status_code == 409  # Present student still has missing reviews.
    await score(c, 0)
    await score(c, 1, "revised")
    created = await prepare(c)
    assert created.status_code == 200, created.text
    first = created.json()["version_id"]
    pending = await c.client.get(root(c) + f"/versions/{first}")
    assert pending.status_code == 200 and pending.json()["result"]["published"] is False
    assert pending.json()["statistics"] is None
    assert (await c.client.get(root(c) + f"/versions/{first}/export.xlsx")).status_code == 404
    approved = await step(c, first, "approve", 1)
    assert approved.status_code == 200, approved.text
    published = await step(c, first, "publish", 2)
    assert published.status_code == 200, published.text
    result = await c.client.get(root(c) + f"/versions/{first}")
    assert result.status_code == 200, result.text
    value = result.json()
    students = value["result"]["students"]
    present = next(s for s in students if s["participation_id"] == c.results_participation)
    assert present["total"] == "18.00" and present["attendance_status"] == "present"
    assert (
        len([s for s in students if s["attendance_status"] == "absent" and s["total"] is None]) == 3
    )
    assert value["result"]["published"] is True
    workbook = await c.client.get(root(c) + f"/versions/{first}/export.xlsx")
    assert workbook.status_code == 200, workbook.text
    with ZipFile(BytesIO(workbook.content)) as archive:
        main = archive.read("xl/worksheets/sheet1.xml")
        assert b"18.00" in main and b"<f" not in main and "缺考".encode() in main
        assert "正式成绩".encode() in archive.read("xl/worksheets/sheet3.xml")
    from huipi_cloud.modules.exam_grading.permissions import MARK_OPERATOR_ROLE

    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        teacher = await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["b"].id,
            role_version_id=UUID(MARK_OPERATOR_ROLE),
            scope_id=scope,
        )
    async with c.factory() as db:
        await access.grant_class(
            db,
            c.principals["admin"],
            uuid4(),
            grant_id=UUID(teacher["grant_id"]),
            session_id=UUID(c.session["id"]),
            class_id=c.classes[0].id,
            export_allowed=True,
        )
    visible = await c.client.get(root(c) + f"/versions/{first}", headers=c.headers("b"))
    assert visible.status_code == 200, visible.text
    assert {s["class_id"] for s in visible.json()["result"]["students"]} == {str(c.classes[0].id)}
    limited_versions = await c.client.get(root(c) + "/versions", headers=c.headers("b"))
    assert limited_versions.status_code == 200
    assert limited_versions.json()["items"][0]["reason"] == "仅考务负责人可见"
    other = f"?class_id={c.classes[1].id}"
    assert (
        await c.client.get(root(c) + f"/versions/{first}" + other, headers=c.headers("b"))
    ).status_code == 404
    assert (
        await c.client.get(
            root(c) + f"/versions/{first}/export.xlsx" + other, headers=c.headers("b")
        )
    ).status_code == 404
    questions = (await c.client.get(f"/api/v1/exam-parsing-jobs/{c.jid}/grading-questions")).json()[
        "questions"
    ]
    c.wid = questions[0]["workspace_id"]
    await review(c, await detail(c), "revised")
    corrected = await prepare(c, first)
    assert corrected.status_code == 200, corrected.text
    second = corrected.json()["version_id"]
    assert (await step(c, second, "approve", 1)).status_code == 200
    assert (await step(c, second, "publish", 2)).status_code == 200
    old = (await c.client.get(root(c) + f"/versions/{first}")).json()
    new = (await c.client.get(root(c) + f"/versions/{second}")).json()
    old_score = next(
        s for s in old["result"]["students"] if s["participation_id"] == c.results_participation
    )["total"]
    new_score = next(
        s for s in new["result"]["students"] if s["participation_id"] == c.results_participation
    )["total"]
    assert old_score == "18.00" and new_score == "16.00"
    assert new["version"]["supersedes_id"] == first
    async with c.factory() as db:
        with pytest.raises(DBAPIError):
            async with db.begin():
                await db.execute(
                    text("UPDATE exam_final_students SET total=0 WHERE version_id=:v"),
                    {"v": UUID(first)},
                )
    from test_organization_migration import alembic

    denied = await alembic(postgres_engine.url, "downgrade", "b18a04f8c019", success=False)
    assert "禁止破坏性降级" in denied.stderr
    history = await c.client.get(root(c) + f"/versions/{first}")
    assert history.status_code == 200 and history.json()["result"]["published"] is True


@pytest.mark.anyio
async def test_explicit_absence_and_permissions(final_context):
    c = final_context
    raw = await c.client.get(root(c) + "/attendance")
    assert raw.status_code == 200
    assert all(r["status"] == "unverified" for r in raw.json()["items"])
    assert (await prepare(c)).status_code == 409
    conflict = await c.client.post(
        root(c) + "/attendance",
        json=dict(
            request_id=str(uuid4()),
            participation_id=c.results_participation,
            expected_revision=0,
            status="absent",
            reason="合成缺考核验",
        ),
    )
    assert conflict.status_code == 409
    denied = await c.client.post(
        root(c) + "/versions",
        headers=c.headers("b"),
        json=dict(
            request_id=str(uuid4()),
            previous_version_id=None,
            reason="未授权",
        ),
    )
    assert denied.status_code == 404


@pytest.mark.anyio
async def test_source_change_blocks_approval_and_replay_is_idempotent(final_context):
    c = final_context
    await set_attendance(c)
    await score(c, 0)
    await score(c, 1)
    body = dict(request_id=str(uuid4()), previous_version_id=None, reason="合成审批")
    first = await c.client.post(root(c) + "/versions", json=body)
    assert first.status_code == 200, first.text
    replay = await c.client.post(root(c) + "/versions", json=body)
    assert replay.status_code == 200 and replay.json() == first.json()
    vid = first.json()["version_id"]
    questions = (await c.client.get(f"/api/v1/exam-parsing-jobs/{c.jid}/grading-questions")).json()[
        "questions"
    ]
    c.wid = questions[0]["workspace_id"]
    await review(c, await detail(c), "revised")
    stale = await step(c, vid, "approve", 1)
    assert stale.status_code == 409 and stale.json()["detail"] == "final_source_changed_reprepare"
    cancelled = await step(c, vid, "cancel", 1)
    assert cancelled.status_code == 200


@pytest.mark.anyio
async def test_attendance_concurrency_and_revoked_replay(final_context):
    import asyncio

    c = final_context
    body = dict(
        request_id=str(uuid4()),
        participation_id=c.results_participation,
        expected_revision=0,
        status="present",
        reason="合成到考核验",
    )
    competing = dict(body, request_id=str(uuid4()))
    replies = await asyncio.gather(
        c.client.post(root(c) + "/attendance", json=body),
        c.client.post(root(c) + "/attendance", json=competing),
    )
    assert sorted(r.status_code for r in replies) == [200, 409]
    winning = body if replies[0].status_code == 200 else competing
    replay = await c.client.post(root(c) + "/attendance", json=winning)
    assert replay.status_code == 200
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.final_grant["grant_id"])
        )
    assert (await c.client.post(root(c) + "/attendance", json=winning)).status_code == 404


@pytest.mark.anyio
async def test_attendance_audit_failure_and_unknown_commit(final_context, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    c = final_context
    body = dict(
        request_id=str(uuid4()),
        participation_id=c.results_participation,
        expected_revision=0,
        status="present",
        reason="合成到考核验",
    )

    def fail_audit(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO exam_final_operations" in statement:
            raise OperationalError("synthetic audit failure", {}, Exception("unavailable"))

    event.listen(c.factory.kw["bind"].sync_engine, "before_cursor_execute", fail_audit)
    try:
        failed = await c.client.post(root(c) + "/attendance", json=body)
        assert failed.status_code == 503
    finally:
        event.remove(c.factory.kw["bind"].sync_engine, "before_cursor_execute", fail_audit)
    async with c.factory() as db:
        assert not (await db.execute(select(final.A))).first()
    original = AsyncSession.commit
    called = False

    async def lost(db):
        nonlocal called
        await original(db)
        if not called:
            called = True
            raise OperationalError("synthetic acknowledgement lost", {}, Exception("lost"))

    monkeypatch.setattr(AsyncSession, "commit", lost)
    uncertain = await c.client.post(root(c) + "/attendance", json=body)
    assert uncertain.status_code == 503
    monkeypatch.setattr(AsyncSession, "commit", original)
    recovered = await c.client.post(root(c) + "/attendance", json=body)
    assert recovered.status_code == 200, recovered.text
    async with c.factory() as db:
        assert len((await db.execute(select(final.A))).all()) == 1
