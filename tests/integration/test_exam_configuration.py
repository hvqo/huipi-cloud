"""Signed JWT and real isolated PostgreSQL: web adapters reuse original transactions."""
# ruff: noqa: F401, F811

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_exam_rosters import auth_api, exam, papers, roster
from test_paper_versions import create, lock

from huipi_cloud.modules.exam_rosters import service as rosters
from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityGrant

BASE = "/api/v1/exam-configuration"


async def post(c, path, **body):
    return await c.client.post(BASE + path, json=body)


async def create_web_session(c, *, ab=False):
    c.client.headers.update(c.headers("admin"))
    body = dict(
        request_id=str(uuid4()),
        academic_term_id=str(c.term_id),
        title="网页合成考试",
        category="midterm",
    )
    r = await post(c, "/events", **body)
    assert r.status_code == 201, r.text
    event = r.json()
    replay = await post(c, "/events", **body)
    assert replay.status_code == 201 and replay.json() == event
    changed = await post(c, "/events", **(body | {"title": "不同正文"}))
    assert changed.status_code == 409
    r = await post(
        c,
        f"/events/{event['id']}/sessions",
        request_id=str(uuid4()),
        expected_event_revision=event["revision"],
        subject_id=str(c.subject_ids[0]),
        title="网页数学场次",
        starts_at="2026-11-01T09:00:00+08:00",
        ends_at="2026-11-01T11:00:00+08:00",
        paper_policy="ab" if ab else "single",
    )
    assert r.status_code == 201, r.text
    return event, r.json()


@pytest.mark.anyio
async def test_browser_contract_event_session_papers_date_roster_freeze_and_correction(roster):
    c = roster
    _, current = await create_web_session(c, ab=True)
    sid = current["id"]
    r = await c.client.get(BASE + "/options")
    assert r.status_code == 200, r.text
    options = r.json()
    assert options["can_create_event"] and options["organization_available"]
    class_choices = list(options["classes"])
    page = options
    offset = 100
    while page["truncated"]:
        assert offset < 1000
        page = (await c.client.get(BASE + "/options", params=dict(offset=offset))).json()
        class_choices.extend(page["classes"])
        offset += 100
    assert {str(cls.id) for cls in c.classes} <= {x["id"] for x in class_choices}
    b = await lock(c, await create(c, title="网页B卷"))
    r = await c.client.get(BASE + "/papers")
    assert r.status_code == 200, r.text
    assert all(
        x["status"] == "locked" and x["rubric_complete"] and x["content_current"]
        for x in r.json()["items"]
    )
    assert "answer_content" not in r.text and "canonical_assignment_id" not in r.text
    for code, pid in [("A", c.paper["paper_version_id"]), ("B", b["paper_version_id"])]:
        r = await post(
            c,
            f"/sessions/{sid}/papers",
            request_id=str(uuid4()),
            expected_revision=current["revision"],
            paper_version_id=pid,
            variant_code=code,
        )
        assert r.status_code == 200, r.text
        current = r.json()
    r = await post(
        c,
        f"/sessions/{sid}/state",
        request_id=str(uuid4()),
        expected_revision=current["revision"],
        lifecycle="ready",
    )
    assert r.status_code == 200, r.text
    current = r.json()
    r = await c.client.get(BASE + f"/sessions/{sid}")
    assert r.status_code == 200, r.text
    detail = r.json()
    bindings = {p["variant_code"]: p["id"] for p in detail["papers"]}
    r = await post(
        c,
        f"/sessions/{sid}/roster-preview",
        request_id=str(uuid4()),
        expected_session_revision=current["revision"],
        class_ids=[str(cls.id) for cls in c.classes],
    )
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["total"] == 3 and len({x["student_id"] for x in preview["students"]}) == 3
    assert preview["business_date"] == "2026-11-01" and all(
        x["exam_number"] is None for x in preview["students"]
    )
    values = [
        dict(
            student_id=x["student_id"],
            exam_number=f"N{i + 1:03}",
            session_paper_id=bindings["A"] if i % 2 == 0 else None,
        )
        for i, x in enumerate(preview["students"])
    ]
    r = await post(
        c,
        f"/sessions/{sid}/rosters",
        request_id=str(uuid4()),
        business_date=preview["business_date"],
        reason="网页合成初始名单",
        entries=values,
    )
    assert r.status_code == 201, r.text
    draft = r.json()
    r = await post(
        c,
        f"/rosters/{draft['id']}/freeze",
        request_id=str(uuid4()),
        expected_revision=draft["revision"],
    )
    assert r.status_code == 200, r.text
    frozen = r.json()
    assert frozen["is_current"] and frozen["status"] == "frozen"
    r = await c.client.get(BASE + f"/rosters/{frozen['id']}")
    assert r.status_code == 200 and r.json()["unknown_variant_count"] == 1
    assert r.json()["attendance_status"] == "not_recorded"
    r = await post(
        c,
        f"/rosters/{frozen['id']}/entries",
        request_id=str(uuid4()),
        expected_revision=frozen["revision"],
        entries=values,
    )
    assert r.status_code == 409
    changed = [dict(x) for x in values]
    changed[0]["exam_number"] = "R001"
    r = await post(
        c,
        f"/rosters/{frozen['id']}/corrections",
        request_id=str(uuid4()),
        expected_revision=frozen["revision"],
        entries=changed,
        reason="网页考号更正",
    )
    assert r.status_code == 201, r.text
    successor = r.json()
    assert successor["previous_revision_id"] == frozen["id"] and not successor["is_current"]
    assert (await c.client.get(BASE + f"/rosters/{frozen['id']}")).json()["roster"]["is_current"]
    r = await post(
        c,
        f"/rosters/{successor['id']}/freeze",
        request_id=str(uuid4()),
        expected_revision=successor["revision"],
    )
    assert r.status_code == 200, r.text
    historical = (await c.client.get(BASE + f"/rosters/{frozen['id']}")).json()
    assert (
        not historical["roster"]["is_current"] and historical["entries"][0]["exam_number"] != "R001"
    )
    # Configuration never grants classroom results access or publishes grades.
    assert (await c.client.get(f"/api/v1/exam-sessions/{sid}/results-preview")).status_code == 404
    async with c.factory() as db:
        from huipi_cloud.modules.exam_results.models import ExamResultClassAccess

        assert not (await db.execute(select(ExamResultClassAccess.__table__))).all()


@pytest.mark.anyio
async def test_unauthorized_legacy_admin_forged_claims_and_cross_session_are_denied(roster):
    c = roster
    _, s = await create_web_session(c)
    c.client.headers.update(c.headers("a"))
    assert (await c.client.get(BASE + "/events")).json()["items"] == []
    assert (await c.client.get(BASE + "/sessions")).json()["items"] == []
    assert not (await c.client.get(BASE + "/options")).json()["can_create_event"]
    assert (await c.client.get(BASE + f"/sessions/{s['id']}")).status_code == 404
    r = await post(
        c,
        "/events",
        request_id=str(uuid4()),
        academic_term_id=str(c.term_id),
        title="未授权",
        role="admin",
        category="other",
    )
    assert r.status_code in {403, 404, 422}
    assert (
        await post(
            c,
            f"/sessions/{s['id']}/state",
            request_id=str(uuid4()),
            expected_revision=1,
            lifecycle="ready",
        )
    ).status_code == 404
    c.client.headers.clear()
    assert (await c.client.get(BASE + "/events")).status_code == 401


@pytest.mark.anyio
async def test_wrong_paper_scope_and_stale_revision_do_not_write(roster):
    c = roster
    _, s = await create_web_session(c)
    sid = s["id"]
    r = await post(
        c,
        f"/sessions/{sid}/papers",
        request_id=str(uuid4()),
        expected_revision=999,
        paper_version_id=c.paper["paper_version_id"],
        variant_code="A",
    )
    assert r.status_code == 409
    r = await post(
        c,
        f"/sessions/{sid}/papers",
        request_id=str(uuid4()),
        expected_revision=s["revision"],
        paper_version_id=str(uuid4()),
        variant_code="A",
    )
    assert r.status_code == 404
    assert not (await c.client.get(BASE + f"/sessions/{sid}")).json()["papers"]
    r = await post(
        c,
        f"/sessions/{sid}/roster-preview",
        request_id=str(uuid4()),
        expected_session_revision=s["revision"],
        class_ids=[str(uuid4())],
    )
    assert r.status_code == 409


@pytest.mark.anyio
async def test_session_scope_list_filter_and_revoked_request_replay(roster):
    from huipi_cloud.modules.exams.permissions import SESSION_MANAGER_ROLE
    from huipi_cloud.modules.resource_security.models import SecurityScope

    c = roster
    _, fresh = await create_web_session(c)
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
            role_version_id=SESSION_MANAGER_ROLE,
            scope_id=scope,
        )
    c.client.headers.update(c.headers("a"))
    listing = await c.client.get(BASE + "/sessions")
    assert listing.status_code == 200 and [r["id"] for r in listing.json()["items"]] == [
        c.session["id"]
    ]
    assert (await c.client.get(BASE + f"/sessions/{fresh['id']}")).status_code == 404
    body = dict(request_id=str(uuid4()), expected_revision=c.session["revision"], lifecycle="ready")
    assert (await post(c, f"/sessions/{c.session['id']}/state", **body)).status_code == 200
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(grant["grant_id"])
        )
    assert (await post(c, f"/sessions/{c.session['id']}/state", **body)).status_code == 404
    assert (await c.client.get(BASE + "/sessions")).json()["items"] == []


@pytest.mark.anyio
async def test_duplicate_numbers_cross_session_binding_and_concurrent_freeze(roster):
    import asyncio

    c = roster
    _, s = await create_web_session(c)
    sid = s["id"]
    r = await post(
        c,
        f"/sessions/{sid}/papers",
        request_id=str(uuid4()),
        expected_revision=1,
        paper_version_id=c.paper["paper_version_id"],
        variant_code="A",
    )
    s = r.json()
    r = await post(
        c,
        f"/sessions/{sid}/state",
        request_id=str(uuid4()),
        expected_revision=s["revision"],
        lifecycle="ready",
    )
    assert r.status_code == 200
    values = [
        dict(student_id=str(x.id), exam_number=f"N{i}", session_paper_id=c.binding["id"])
        for i, x in enumerate(c.students)
    ]
    body = dict(
        request_id=str(uuid4()), business_date="2026-11-01", reason="负向测试", entries=values
    )
    # SessionPaper belongs to the original session, never the new one.
    assert (await post(c, f"/sessions/{sid}/rosters", **body)).status_code == 409
    binding = (await c.client.get(BASE + f"/sessions/{sid}")).json()["papers"][0]["id"]
    for x in values:
        x["session_paper_id"] = binding
    values[1]["exam_number"] = values[0]["exam_number"]
    assert (
        await post(c, f"/sessions/{sid}/rosters", **(body | dict(request_id=str(uuid4()))))
    ).status_code == 422
    values[1]["exam_number"] = "N9"
    r = await post(c, f"/sessions/{sid}/rosters", **(body | dict(request_id=str(uuid4()))))
    assert r.status_code == 201, r.text
    draft = r.json()
    responses = await asyncio.gather(
        *[
            post(
                c,
                f"/rosters/{draft['id']}/freeze",
                request_id=str(uuid4()),
                expected_revision=draft["revision"],
            )
            for _ in range(2)
        ]
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    async with c.factory() as db:
        from sqlalchemy import func

        assert (
            await db.scalar(
                select(func.count())
                .select_from(rosters.R)
                .where(rosters.R.c.exam_session_id == UUID(sid), rosters.R.c.is_current)
            )
            == 1
        )


@pytest.mark.anyio
async def test_audit_failure_database_unavailable_and_bounded_input(roster):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    c = roster
    c.client.headers.update(c.headers("admin"))
    engine = c.factory.kw["bind"].sync_engine

    def audit_fail(conn, cursor, statement, params, context, many):
        if statement.startswith("INSERT INTO exam_operations"):
            raise OperationalError("synthetic failure", {}, Exception("unavailable"))

    event.listen(engine, "before_cursor_execute", audit_fail)
    title = "回滚合成考试"
    try:
        r = await post(
            c,
            "/events",
            request_id=str(uuid4()),
            academic_term_id=str(c.term_id),
            title=title,
            category="other",
        )
        assert r.status_code == 503
    finally:
        event.remove(engine, "before_cursor_execute", audit_fail)
    async with c.factory() as db:
        assert await db.scalar(select(exams.E.c.id).where(exams.E.c.title == title)) is None

    async def oversized():
        yield b'{"request_id":"'
        for _ in range(18):
            yield b"x" * 65536

    r = await c.client.post(
        BASE + "/events", content=oversized(), headers={"content-type": "application/json"}
    )
    assert r.status_code == 413


@pytest.mark.anyio
async def test_paper_use_exposes_metadata_only_and_draft_retired_binding_fails(roster):
    from test_paper_versions import write

    from huipi_cloud.modules.exams.permissions import PAPER_USER_ROLE
    from huipi_cloud.modules.papers import service as paper_service
    from huipi_cloud.modules.resource_security.models import SecurityScope

    c = roster
    _, s = await create_web_session(c)
    sid = s["id"]
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(
                SecurityScope.paper_version_id == UUID(c.paper["paper_version_id"])
            )
        )
    async with c.factory() as db:
        await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=PAPER_USER_ROLE,
            scope_id=scope,
        )
    c.client.headers.update(
        c.headers("a", role="admin", is_admin=True, teacher_id=str(c.users["admin"].id))
    )
    r = await c.client.get(BASE + "/papers")
    assert r.status_code == 200 and len(r.json()["items"]) == 1
    assert r.json()["items"][0]["question_count"] == 2
    assert all(
        word not in r.text
        for word in ["answer_content", "stem", "canonical_assignment", "object_key", "criteria"]
    )
    assert (await c.client.get(BASE + f"/sessions/{sid}")).status_code == 404
    assert (
        await c.client.get(f"/api/v1/assignments/{c.paper['canonical_assignment_id']}")
    ).status_code == 404
    c.client.headers.update(c.headers("admin"))
    draft = await create(c, title="未锁定网页合成卷")
    r = await post(
        c,
        f"/sessions/{sid}/papers",
        request_id=str(uuid4()),
        expected_revision=s["revision"],
        paper_version_id=draft["paper_version_id"],
        variant_code="A",
    )
    assert r.status_code == 409
    async with c.factory() as db:
        await paper_service.retire_paper(db, c.principals["admin"], write(c.paper))
    r = await post(
        c,
        f"/sessions/{sid}/papers",
        request_id=str(uuid4()),
        expected_revision=s["revision"],
        paper_version_id=c.paper["paper_version_id"],
        variant_code="A",
    )
    assert r.status_code == 409
    old = (await c.client.get(BASE + f"/sessions/{c.session['id']}")).json()
    assert (
        old["papers"][0]["paper_status"] == "retired" and old["papers"][0]["id"] == c.binding["id"]
    )
