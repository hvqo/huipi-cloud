"""Actual signed JWT, PostgreSQL and protected paper services through HTTP."""
# ruff: noqa: F401, F811

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from test_exam_rosters import exam, roster
from test_paper_versions import auth_api, create, lock, papers, write

from huipi_cloud.modules.identity.models import AssignmentMembership
from huipi_cloud.modules.papers import service as service
from huipi_cloud.modules.papers.models import PaperOperation, PaperVersion
from huipi_cloud.modules.papers.permissions import PAPER_EDITOR_ROLE, PAPER_READER_ROLE
from huipi_cloud.modules.resource_security import service as grants
from huipi_cloud.modules.resource_security.models import SecurityGrant, SecurityScope

BASE = "/api/v1/papers"


async def member(c):
    c.client.headers.update(c.headers("admin"))
    # Deployment fixture: management HTTP intentionally cannot enroll admin as a teacher.
    async with c.factory() as db, db.begin():
        old = await db.scalar(
            select(AssignmentMembership.id).where(
                AssignmentMembership.assignment_id == c.aid,
                AssignmentMembership.user_id == c.users["admin"].id,
            )
        )
        if old is None:
            db.add(
                AssignmentMembership(
                    assignment_id=c.aid, user_id=c.users["admin"].id, can_write=False
                )
            )


async def snapshot(c, title="网页试卷"):
    await member(c)
    body = dict(request_id=str(uuid4()), source_assignment_id=str(c.aid), title=title)
    r = await c.client.post(BASE, json=body)
    assert r.status_code == 201, r.text
    replay = await c.client.post(BASE, json=body)
    assert replay.status_code == 201 and replay.json() == r.json()
    return r.json()


async def detail(c, pid):
    r = await c.client.get(BASE + f"/{pid}")
    assert r.status_code == 200, r.text
    return r.json()


def version(d):
    p = d["paper"]
    return dict(
        request_id=str(uuid4()),
        expected_revision=p["revision"],
        expected_content_sha256=p["expected_content_sha256"],
        expected_mapping_digest=p["expected_mapping_digest"],
    )


def edit(d):
    q = d["questions"][0]
    return dict(
        **version(d),
        question_key=q["question_key"],
        question=q["question"],
        answer_content="四",
        rubric=q["rubric"],
    )


@pytest.mark.anyio
async def test_web_import_edit_reorder_lock_fork_and_old_carrier_isolation(papers):
    c = papers
    value = await snapshot(c)
    pid = value["paper_version_id"]
    d = await detail(c, pid)
    assert all(d["paper"]["checks"].values())
    assert "canonical_assignment_id" not in str(d) and "legacy_question_id" not in str(d)
    request = edit(d)
    r = await c.client.post(BASE + f"/{pid}/questions", json=request)
    assert r.status_code == 200, r.text
    assert (await c.client.post(BASE + f"/{pid}/questions", json=request)).json() == r.json()
    assert (await c.client.post(BASE + f"/{pid}/questions", json=edit(d))).status_code == 409
    d = await detail(c, pid)
    assert d["questions"][0]["answer_content"] == "四"
    ordered = [
        dict(question_key=q["question_key"], printed_label=f"新{i}")
        for i, q in enumerate(reversed(d["questions"]), 1)
    ]
    r = await c.client.post(BASE + f"/{pid}/order", json=dict(**version(d), mappings=ordered))
    assert r.status_code == 200, r.text
    d = await detail(c, pid)
    assert [q["question_key"] for q in d["questions"]] == [q["question_key"] for q in ordered]
    r = await c.client.post(BASE + f"/{pid}/lock", json=version(d))
    assert r.status_code == 200, r.text
    frozen = await detail(c, pid)
    assert frozen["paper"]["usable_for_exam"]
    assert (await c.client.post(BASE + f"/{pid}/questions", json=edit(frozen))).status_code == 409
    async with c.factory() as db:
        row = await service.row(db, UUID(pid))
        carrier = row["canonical_assignment_id"]
        await db.rollback()
        with pytest.raises(DBAPIError):
            async with db.begin():
                await db.execute(
                    update(service.Q)
                    .where(service.Q.c.assignment_id == carrier)
                    .values(stem="illegal")
                )
    assert (await c.client.get(f"/api/v1/assignments/{carrier}")).status_code == 404
    fork = await c.client.post(BASE + f"/{pid}/fork", json=version(frozen))
    assert fork.status_code == 201, fork.text
    child = await detail(c, fork.json()["paper_version_id"])
    assert child["paper"]["prior_version_id"] == pid and child["paper"]["version_no"] == 2
    assert (await detail(c, pid)) == frozen
    assert len((await c.client.get(BASE + f"/{pid}/history")).json()["items"]) == 2


@pytest.mark.anyio
async def test_web_import_requires_explicit_member_even_for_old_admin_and_published_source(papers):
    c = papers
    c.client.headers.update(c.headers("admin"))
    assert (await c.client.get(f"/api/v1/assignments/{c.aid}")).status_code == 200
    assert (await c.client.get(BASE + "/sources")).json()["items"] == []
    body = dict(request_id=str(uuid4()), source_assignment_id=str(c.aid), title="不允许")
    assert (await c.client.post(BASE, json=body)).status_code == 404
    await member(c)
    assert str(c.aid) in str((await c.client.get(BASE + "/sources")).json())
    async with c.factory() as db, db.begin():
        await db.execute(update(service.A).where(service.A.c.id == c.aid).values(status="draft"))
    assert (await c.client.post(BASE, json=body)).status_code == 409
    assert (await c.client.get(BASE + "/sources")).json()["items"] == []
    async with c.factory() as db:
        assert not (await db.execute(select(PaperVersion.id))).all()


@pytest.mark.anyio
async def test_use_only_or_jwt_admin_does_not_reveal_answers_or_family_history(papers):
    from huipi_cloud.modules.exams import service as exams
    from huipi_cloud.modules.exams.permissions import PAPER_USER_ROLE

    c = papers
    value = await snapshot(c)
    pid = value["paper_version_id"]
    async with c.factory() as db:
        await exams.enable_operator(
            db, c.users["admin"].id, c.users["admin"].id, uuid4(), "synthetic use test"
        )
    async with c.factory() as db:
        await grants.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=PAPER_USER_ROLE,
            scope_id=grants.INSTALLATION_SCOPE,
        )
    c.client.headers.update(c.headers("a", role="admin", is_admin=True))
    r = await c.client.get(BASE)
    assert r.status_code == 200 and r.json()["items"] == []
    assert not r.json()["can_create"]
    for path in [f"/{pid}", f"/{pid}/history", "/sources"]:
        r = await c.client.get(BASE + path)
        assert r.status_code == 404 and "answer_content" not in r.text
    assert (await c.client.get(BASE + "/" + str(uuid4()))).status_code == 404
    c.client.headers.clear()
    assert (await c.client.get(BASE)).status_code == 401


@pytest.mark.anyio
async def test_paper_scope_edit_has_no_lock_permission_and_revoked_replay_is_denied(papers):
    c = papers
    v = await snapshot(c)
    pid = v["paper_version_id"]
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.paper_version_id == UUID(pid))
        )
        await db.rollback()
        granted = await grants.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=PAPER_EDITOR_ROLE,
            scope_id=scope,
        )
    c.client.headers.update(c.headers("a"))
    d = await detail(c, pid)
    assert (await c.client.post(BASE + f"/{pid}/lock", json=version(d))).status_code == 404
    request = edit(d)
    r = await c.client.post(BASE + f"/{pid}/questions", json=request)
    assert r.status_code == 200, r.text
    async with c.factory() as db:
        await grants.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(granted["grant_id"])
        )
    assert (await c.client.post(BASE + f"/{pid}/questions", json=request)).status_code == 404
    assert (await c.client.get(BASE + f"/{pid}")).status_code == 404


@pytest.mark.anyio
async def test_concurrent_http_edits_and_invalid_rubric_or_mapping_do_not_partially_write(papers):
    c = papers
    pid = (await snapshot(c))["paper_version_id"]
    d = await detail(c, pid)
    bad = edit(d)
    bad["rubric"] = {"criteria": [dict(description="不等于满分", points="1.00", sort_order=1)]}
    assert (await c.client.post(BASE + f"/{pid}/questions", json=bad)).status_code == 422
    duplicate = [dict(question_key=d["questions"][0]["question_key"], printed_label="1")] * 2
    assert (
        await c.client.post(BASE + f"/{pid}/order", json=dict(**version(d), mappings=duplicate))
    ).status_code == 422
    req1, req2 = edit(d), edit(d)
    req2["answer_content"] = "另一份标准答案"
    replies = await asyncio.gather(
        c.client.post(BASE + f"/{pid}/questions", json=req1),
        c.client.post(BASE + f"/{pid}/questions", json=req2),
    )
    assert sorted(r.status_code for r in replies) == [200, 409]
    after = await detail(c, pid)
    assert after["paper"]["revision"] == d["paper"]["revision"] + 1


@pytest.mark.anyio
async def test_import_audit_failure_rolls_back_and_lost_commit_ack_replays(
    papers, postgres_engine, monkeypatch
):
    c = papers
    await member(c)
    request = dict(request_id=str(uuid4()), source_assignment_id=str(c.aid), title="故障试卷")

    def fail(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO paper_operations" in statement:
            raise OperationalError("redacted", None, Exception("audit failure"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail)
    try:
        assert (await c.client.post(BASE, json=request)).status_code == 503
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail)
    async with c.factory() as db:
        assert not (await db.execute(select(PaperVersion.id))).all()
    commit = c.factory.class_.commit
    original = service.create_member_snapshot
    armed = True

    async def lost_ack(db):
        nonlocal armed
        await commit(db)
        if db.info.pop("lost_ack", False) and armed:
            armed = False
            raise OSError("synthetic commit acknowledgement lost")

    async def marking(db, principal, payload):
        db.info["lost_ack"] = True
        return await original(db, principal, payload)

    monkeypatch.setattr(c.factory.class_, "commit", lost_ack)
    monkeypatch.setattr(service, "create_member_snapshot", marking)
    unknown = await c.client.post(BASE, json=request)
    assert unknown.status_code == 503 and unknown.json()["detail"] == "paper_commit_outcome_unknown"
    r = await c.client.post(BASE, json=request)
    assert r.status_code == 201, r.text
    replay = await c.client.post(BASE, json=request)
    assert replay.status_code == 201 and replay.json() == r.json()
    async with c.factory() as db:
        assert len((await db.execute(select(PaperVersion.id))).all()) == 1


@pytest.mark.anyio
async def test_web_retirement_preserves_existing_session_paper(roster):
    from huipi_cloud.modules.exams.models import SessionPaper

    c = roster
    c.client.headers.update(c.headers("admin"))
    pid = c.paper["paper_version_id"]
    d = await detail(c, pid)
    async with c.factory() as db:
        before = [dict(r) for r in (await db.execute(select(SessionPaper.__table__))).mappings()]
    assert before
    r = await c.client.post(BASE + f"/{pid}/retire", json=version(d))
    assert r.status_code == 200, r.text
    after = await detail(c, pid)
    assert after["paper"]["status"] == "retired" and not after["paper"]["usable_for_exam"]
    assert after["questions"] == d["questions"]
    async with c.factory() as db:
        assert [
            dict(r) for r in (await db.execute(select(SessionPaper.__table__))).mappings()
        ] == before


@pytest.mark.anyio
async def test_family_history_filters_every_version_and_creation_does_not_grant(papers):
    c = papers
    pid = (await snapshot(c))["paper_version_id"]
    d = await detail(c, pid)
    r = await c.client.post(BASE + f"/{pid}/lock", json=version(d))
    assert r.status_code == 200
    original = await detail(c, pid)
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.paper_version_id == UUID(pid))
        )
    async with c.factory() as db:
        await grants.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["reader"].id,
            role_version_id=PAPER_READER_ROLE,
            scope_id=scope,
        )
    async with c.factory() as db:
        before = set((await db.scalars(select(SecurityGrant.id))).all())
    r = await c.client.post(BASE + f"/{pid}/fork", json=version(original))
    assert r.status_code == 201
    successor = r.json()["paper_version_id"]
    async with c.factory() as db:
        assert set((await db.scalars(select(SecurityGrant.id))).all()) == before
    c.client.headers.update(c.headers("reader"))
    history = await c.client.get(BASE + f"/{pid}/history")
    assert history.status_code == 200
    assert [p["id"] for p in history.json()["items"]] == [pid]
    assert successor not in history.text
    assert (await c.client.get(BASE + f"/{successor}")).status_code == 404
