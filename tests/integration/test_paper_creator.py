"""Signed teacher HTTP + isolated PostgreSQL/S3: no operator grants on the teacher."""
# ruff: noqa: F401, F811

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, insert, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from test_exam_sessions import exam
from test_paper_versions import auth_api, papers
from test_paper_workbench import BASE, detail, edit, version

from huipi_cloud.modules.identity.models import AssignmentMembership
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.papers import service as paper
from huipi_cloud.modules.papers.models import PaperCreatorAuthority, PaperOperation, PaperVersion
from huipi_cloud.modules.papers.permissions import PAPER_AUTHOR_ROLE, PAPER_CREATOR_ROLE
from huipi_cloud.modules.resource_security import service as grants
from huipi_cloud.modules.resource_security.models import (
    SecurityGrant,
    SecurityRolePermission,
    SecurityRoleVersion,
    SecurityScope,
)


async def provision(c, name="a"):
    async with c.factory() as db:
        return await grants.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users[name].id,
            role_version_id=PAPER_CREATOR_ROLE,
            scope_id=grants.INSTALLATION_SCOPE,
        )


async def create(c, title="普通教师命题", request=None):
    c.client.headers.update(c.headers("a"))
    request = request or dict(request_id=str(uuid4()), source_assignment_id=str(c.aid), title=title)
    return await c.client.post(BASE, json=request)


async def authorities(c):
    async with c.factory() as db:
        return [
            dict(r) for r in (await db.execute(select(PaperCreatorAuthority.__table__))).mappings()
        ]


async def revoke(c, gid):
    async with c.factory() as db:
        await grants.revoke_grant(db, c.principals["admin"], uuid4(), grant_id=UUID(str(gid)))


@pytest.mark.anyio
async def test_creator_exact_roles_and_end_to_end_snapshot_edit_lock_fork(papers):
    c = papers
    await provision(c)
    async with c.factory() as db:
        assert (await db.get(SecurityRoleVersion, PAPER_CREATOR_ROLE)).sealed_at
        assert await grants.role_codes(db, PAPER_CREATOR_ROLE, "installation") == {"paper.create"}
        assert await grants.role_codes(db, PAPER_AUTHOR_ROLE, "paper_version") == {
            "paper.read",
            "paper.edit",
            "paper.lock",
        }
    c.client.headers.update(c.headers("a"))
    sources = await c.client.get(BASE + "/sources")
    assert [i["id"] for i in sources.json()["items"]] == [str(c.aid)]
    r = await create(c)
    assert r.status_code == 201, r.text
    pid = r.json()["paper_version_id"]
    d = await detail(c, pid)
    assert set(d["paper"]["actions"]) == {"paper.read", "paper.edit", "paper.lock"}
    assert (await c.client.post(BASE + f"/{pid}/questions", json=edit(d))).status_code == 200
    d = await detail(c, pid)
    ordered = [
        dict(question_key=q["question_key"], printed_label=f"题{i}")
        for i, q in enumerate(reversed(d["questions"]), 1)
    ]
    assert (
        await c.client.post(BASE + f"/{pid}/order", json=dict(**version(d), mappings=ordered))
    ).status_code == 200
    d = await detail(c, pid)
    assert (await c.client.post(BASE + f"/{pid}/lock", json=version(d))).status_code == 200
    locked = await detail(c, pid)
    fork = await c.client.post(BASE + f"/{pid}/fork", json=version(locked))
    assert fork.status_code == 201, fork.text
    child = await detail(c, fork.json()["paper_version_id"])
    assert child["paper"]["status"] == "draft"
    assert child["paper"]["prior_version_id"] == pid
    assert (await detail(c, pid)) == locked
    rows = await authorities(c)
    assert len(rows) == 2 and all(row["actor_user_id"] == c.users["a"].id for row in rows)
    async with c.factory() as db:
        facts = (
            (
                await db.execute(
                    select(
                        SecurityGrant.__table__, SecurityScope.kind, SecurityScope.paper_version_id
                    )
                    .join(SecurityScope, SecurityGrant.scope_id == SecurityScope.id)
                    .where(SecurityGrant.user_id == c.users["a"].id)
                )
            )
            .mappings()
            .all()
        )
        assert len(facts) == 3
        assert [(r["role_version_id"], r["kind"]) for r in facts].count(
            (PAPER_CREATOR_ROLE, "installation")
        ) == 1
        assert all(
            r["role_version_id"] == PAPER_AUTHOR_ROLE and r["kind"] == "paper_version"
            for r in facts
            if r["role_version_id"] != PAPER_CREATOR_ROLE
        )
        for row in rows:
            op = await db.get(PaperOperation, row["operation_id"])
            assert op.actor_user_id == c.users["a"].id
            assert op.result["paper_version_id"] == str(row["paper_version_id"])
    for path in ["/api/v1/admin/users", "/api/v1/exam-configuration/organization"]:
        denied = await c.client.get(path)
        assert denied.status_code in {403, 404}, denied.text


@pytest.mark.anyio
async def test_no_source_or_object_or_grant_management_escalation(papers):
    c = papers
    await provision(c, "b")
    c.client.headers.update(c.headers("b", role="admin", is_admin=True))
    assert (await c.client.get(BASE + "/sources")).json()["items"] == []
    request = dict(request_id=str(uuid4()), source_assignment_id=str(c.aid), title="无来源权限")
    assert (await c.client.post(BASE, json=request)).status_code == 404
    await provision(c)
    pid = (await create(c)).json()["paper_version_id"]
    c.client.headers.update(c.headers("b"))
    assert (await c.client.get(BASE)).json()["items"] == []
    for path in [f"/{pid}", f"/{pid}/history"]:
        assert (await c.client.get(BASE + path)).status_code == 404
    async with c.factory() as db:
        with pytest.raises(AccessError):
            await grants.grant_role(
                db,
                c.principals["b"],
                uuid4(),
                user_id=c.users["a"].id,
                role_version_id=PAPER_CREATOR_ROLE,
                scope_id=grants.INSTALLATION_SCOPE,
            )
    async with c.factory() as db:
        with pytest.raises(AccessError):
            await grants.grant_role(
                db,
                c.principals["a"],
                uuid4(),
                user_id=c.users["a"].id,
                role_version_id=PAPER_CREATOR_ROLE,
                scope_id=grants.INSTALLATION_SCOPE,
            )


@pytest.mark.anyio
async def test_replay_dedup_and_concurrent_create_never_add_extra_authority(papers):
    c = papers
    await provision(c)
    body = dict(request_id=str(uuid4()), source_assignment_id=str(c.aid), title="幂等并发")
    r = await create(c, request=body)
    assert r.status_code == 201, r.text
    for request in [body, dict(body, request_id=str(uuid4()))]:
        assert (await create(c, request=request)).json() == r.json()
    assert (await create(c, request=dict(body, title="冲突"))).status_code == 409
    assert len(await authorities(c)) == 1
    title = "两个不同请求同时创建"
    a, b = await asyncio.gather(create(c, title), create(c, title))
    assert a.status_code == b.status_code == 201, (a.text, b.text)
    assert a.json() == b.json()
    assert len(await authorities(c)) == 2
    # Another member with create but not read cannot claim an already-existing family/version.
    await provision(c, "b")
    async with c.factory() as db, db.begin():
        db.add(AssignmentMembership(assignment_id=c.aid, user_id=c.users["b"].id, can_write=False))
    c.client.headers.update(c.headers("b"))
    assert (await c.client.post(BASE, json=dict(body, request_id=str(uuid4())))).status_code == 404
    assert len(await authorities(c)) == 2
    async with c.factory() as db:
        assert not (
            await db.scalars(
                select(SecurityGrant.id).where(
                    SecurityGrant.user_id == c.users["b"].id,
                    SecurityGrant.role_version_id == PAPER_AUTHOR_ROLE,
                )
            )
        ).all()


@pytest.mark.anyio
async def test_revoked_creation_and_object_grants_deny_new_work_and_replay(papers):
    c = papers
    root = await provision(c)
    body = dict(request_id=str(uuid4()), source_assignment_id=str(c.aid), title="撤权")
    pid = (await create(c, request=body)).json()["paper_version_id"]
    d = await detail(c, pid)
    await revoke(c, root["grant_id"])
    assert (await create(c, request=body)).status_code == 404
    assert (await create(c, "撤权后建卷")).status_code == 404
    assert (await c.client.get(BASE + f"/{pid}")).status_code == 200
    assert (await c.client.post(BASE + f"/{pid}/lock", json=version(d))).status_code == 200
    d = await detail(c, pid)
    assert (await c.client.post(BASE + f"/{pid}/fork", json=version(d))).status_code == 404
    await provision(c)
    rows = await authorities(c)
    await revoke(c, rows[0]["grant_id"])
    assert (await c.client.get(BASE + f"/{pid}")).status_code == 404
    for suffix in ["lock", "fork"]:
        assert (await c.client.post(BASE + f"/{pid}/{suffix}", json=version(d))).status_code == 404
    # Root create alone cannot resurrect a revoked object grant via new request or replay.
    assert (await create(c, request=dict(body, request_id=str(uuid4())))).status_code == 404
    # Original creation replay also requires current object access.
    replay = await create(c, request=body)
    assert replay.status_code == 404
    assert len(await authorities(c)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "table", ["security_grants", "paper_creator_authorities", "paper_operations"]
)
async def test_authority_or_audit_failure_rolls_back_everything(papers, postgres_engine, table):
    c = papers
    await provision(c)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO " + table + " " in statement:
            raise OperationalError("redacted", None, Exception("synthetic authority fault"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail)
    try:
        assert (await create(c)).status_code == 503
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail)
    async with c.factory() as db:
        assert not (await db.scalars(select(PaperVersion.id))).all()
        assert not (await db.scalars(select(paper.F.c.id))).all()
        assert not (
            await db.scalars(
                select(paper.A.c.id).where(paper.A.c.security_classification == "exam_protected")
            )
        ).all()
        assert not (await db.scalars(select(PaperCreatorAuthority.paper_version_id))).all()
        assert not (
            await db.scalars(
                select(PaperOperation.id).where(PaperOperation.actor_user_id == c.users["a"].id)
            )
        ).all()
        assert not (
            await db.scalars(
                select(SecurityGrant.id).where(
                    SecurityGrant.user_id == c.users["a"].id,
                    SecurityGrant.role_version_id == PAPER_AUTHOR_ROLE,
                )
            )
        ).all()


@pytest.mark.anyio
async def test_unknown_commit_recovers_the_same_grant_and_immutable_audit(papers, monkeypatch):
    c = papers
    await provision(c)
    commit = c.factory.class_.commit
    original = paper.create_member_snapshot
    armed = True

    async def lost_ack(db):
        nonlocal armed
        await commit(db)
        if db.info.pop("lost_creator_ack", False) and armed:
            armed = False
            raise OSError("synthetic lost acknowledgement")

    async def injected(db, *args, **kwargs):
        db.info["lost_creator_ack"] = True
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(c.factory.class_, "commit", lost_ack)
    monkeypatch.setattr(paper, "create_member_snapshot", injected)
    body = dict(request_id=str(uuid4()), source_assignment_id=str(c.aid), title="未知提交")
    first = await create(c, request=body)
    assert first.status_code == 503 and first.json()["detail"] == "paper_commit_outcome_unknown"
    rows = await authorities(c)
    assert len(rows) == 1
    replay = await create(c, request=body)
    assert replay.status_code == 201
    assert (await authorities(c)) == rows
    async with c.factory() as db:
        assert await db.get(PaperVersion, UUID(replay.json()["paper_version_id"]))
    for sql in [
        "DELETE FROM paper_creator_authorities",
        "UPDATE paper_creator_authorities SET actor_user_id=actor_user_id",
    ]:
        async with c.factory() as db:
            with pytest.raises(DBAPIError):
                async with db.begin():
                    await db.execute(text(sql))


@pytest.mark.anyio
async def test_author_role_cannot_be_delegated_or_inserted_for_existing_version(papers):
    c = papers
    await provision(c)
    pid = (await create(c)).json()["paper_version_id"]
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.paper_version_id == UUID(pid))
        )
    async with c.factory() as db:
        with pytest.raises(AccessError, match="paper_author_role_creation_only"):
            await grants.grant_role(
                db,
                c.principals["admin"],
                uuid4(),
                user_id=c.users["b"].id,
                role_version_id=PAPER_AUTHOR_ROLE,
                scope_id=scope,
            )
    for uid, role, sid in [
        (c.users["a"].id, PAPER_AUTHOR_ROLE, scope),
        (c.users["b"].id, PAPER_AUTHOR_ROLE, scope),
        (c.users["a"].id, PAPER_AUTHOR_ROLE, grants.INSTALLATION_SCOPE),
        (c.users["b"].id, PAPER_CREATOR_ROLE, scope),
    ]:
        async with c.factory() as db:
            with pytest.raises(DBAPIError):
                async with db.begin():
                    await db.execute(
                        insert(SecurityGrant).values(
                            id=uuid4(), user_id=uid, role_version_id=role, scope_id=sid
                        )
                    )


@pytest.mark.anyio
async def test_two_members_racing_same_family_only_actual_creator_gets_authority(papers):
    c = papers
    for name in ("a", "b"):
        await provision(c, name)
    async with c.factory() as db, db.begin():
        db.add(AssignmentMembership(assignment_id=c.aid, user_id=c.users["b"].id, can_write=False))
    body = dict(source_assignment_id=str(c.aid), title="双教师竞争同一来源与家族")
    replies = await asyncio.gather(
        *[
            c.client.post(BASE, headers=c.headers(name), json=dict(body, request_id=str(uuid4())))
            for name in ("a", "b")
        ]
    )
    assert sum(r.status_code == 201 for r in replies) == 1, [r.text for r in replies]
    assert all(r.status_code in {201, 404, 409} for r in replies)
    rows = await authorities(c)
    assert len(rows) == 1
    winner = "a" if replies[0].status_code == 201 else "b"
    assert rows[0]["actor_user_id"] == c.users[winner].id


@pytest.mark.anyio
async def test_creation_and_revocation_serialize_then_new_work_is_denied(papers, monkeypatch):
    from test_paper_versions import wait_for_pg_lock

    c = papers
    root = await provision(c)
    held, release = asyncio.Event(), asyncio.Event()
    original = paper.clone

    async def pause(*args, **kwargs):
        result = await original(*args, **kwargs)
        held.set()
        await release.wait()
        return result

    monkeypatch.setattr(paper, "clone", pause)
    creating = asyncio.create_task(create(c, "撤权提交竞争"))
    await asyncio.wait_for(held.wait(), 10)
    revoking = asyncio.create_task(revoke(c, root["grant_id"]))
    try:
        await wait_for_pg_lock(c.factory, "security_grants")
    finally:
        release.set()
    result = await creating
    await revoking
    assert result.status_code == 201, result.text
    assert len(await authorities(c)) == 1
    assert (await create(c, "撤权后另建")).status_code == 404


@pytest.mark.anyio
async def test_deferred_database_check_rejects_authority_with_wrong_audit_result(
    papers, monkeypatch
):
    c = papers
    await provision(c)
    original = paper.clone

    async def forged(*args, **kwargs):
        result = await original(*args, **kwargs)
        return dict(result, paper_version_id=str(uuid4()))

    monkeypatch.setattr(paper, "clone", forged)
    # Actual COMMIT invokes PostgreSQL's deferred join through real FK identities.
    r = await create(c)
    assert r.status_code == 503 and r.json()["detail"] == "paper_commit_outcome_unknown"
    assert await authorities(c) == []
    async with c.factory() as db:
        assert not (await db.scalars(select(PaperVersion.id))).all()
        assert not (
            await db.scalars(
                select(SecurityGrant.id).where(SecurityGrant.role_version_id == PAPER_AUTHOR_ROLE)
            )
        ).all()


@pytest.mark.anyio
async def test_object_authority_does_not_open_exam_roster_results_or_use(exam):
    from test_exam_sessions import new_session

    c = exam
    session = await new_session(c)
    sid, eid = session["id"], c.event["id"]
    await provision(c)
    assert (await create(c)).status_code == 201
    c.client.headers.update(c.headers("a", role="admin", is_admin=True))
    for path in [
        "/exam-configuration/events",
        "/exam-configuration/sessions",
        "/exam-configuration/papers",
        "/exam-result-sessions",
    ]:
        r = await c.client.get("/api/v1" + path)
        assert r.status_code == 200 and r.json()["items"] == [], r.text
    for path in [
        f"/exam-configuration/events/{eid}",
        f"/exam-configuration/sessions/{sid}",
        f"/exam-configuration/sessions/{sid}/rosters",
        f"/exam-sessions/{sid}/results-preview",
        f"/exam-sessions/{sid}/results-preview.xlsx",
    ]:
        r = await c.client.get("/api/v1" + path)
        assert r.status_code == 404 and not r.content.startswith(b"PK"), r.text
    r = await c.client.post(
        "/api/v1/exam-configuration/events",
        json=dict(request_id=str(uuid4()), academic_term_id=str(c.term_id), title="不可越权建考试"),
    )
    assert r.status_code == 404, r.text


@pytest.mark.anyio
async def test_revoked_successor_cannot_be_read_through_parent_fork_replay(papers):
    c = papers
    await provision(c)
    pid = (await create(c)).json()["paper_version_id"]
    d = await detail(c, pid)
    assert (await c.client.post(BASE + f"/{pid}/lock", json=version(d))).status_code == 200
    d = await detail(c, pid)
    request = version(d)
    result = await c.client.post(BASE + f"/{pid}/fork", json=request)
    assert result.status_code == 201, result.text
    child = result.json()["paper_version_id"]
    receipt = next(r for r in await authorities(c) if str(r["paper_version_id"]) == child)
    await revoke(c, receipt["grant_id"])
    assert (await detail(c, pid))["paper"]["status"] == "locked"
    assert (await c.client.get(BASE + f"/{child}")).status_code == 404
    assert (await c.client.post(BASE + f"/{pid}/fork", json=request)).status_code == 404
    assert len(await authorities(c)) == 2


@pytest.mark.anyio
async def test_creator_approval_cli_verifies_admin_jwt_and_teacher_cannot_self_approve(
    papers,
    monkeypatch,
):
    import argparse
    import io
    import sys

    from huipi_cloud.modules.resource_security import __main__ as cli

    c = papers
    monkeypatch.setattr(cli, "session_factory", c.factory)
    monkeypatch.setattr(cli, "TokenVerifier", lambda config: c.verifier)
    args = argparse.Namespace(
        command="grant",
        token_stdin=True,
        request_id=uuid4(),
        user_id=c.users["a"].id,
        role_version_id=PAPER_CREATOR_ROLE,
        scope_id=grants.INSTALLATION_SCOPE,
    )
    admin_token = c.headers("admin")["Authorization"].split(" ", 1)[1]
    monkeypatch.setattr(sys, "stdin", io.StringIO(admin_token + "\n"))
    granted = await cli.run(args)
    monkeypatch.setattr(sys, "stdin", io.StringIO(admin_token + "\n"))
    assert await cli.run(args) == granted
    teacher_token = c.headers("a", role="admin", is_admin=True)["Authorization"].split(" ", 1)[1]
    args.request_id = uuid4()
    monkeypatch.setattr(sys, "stdin", io.StringIO(teacher_token + "\n"))
    with pytest.raises(AccessError):
        await cli.run(args)
    async with c.factory() as db:
        assert (
            len(
                (
                    await db.scalars(
                        select(SecurityGrant.id).where(SecurityGrant.user_id == c.users["a"].id)
                    )
                ).all()
            )
            == 1
        )
