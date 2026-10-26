"""Current signed identities, real isolated PG/S3; no paid model calls."""

# ruff: noqa: F401,F811
import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from test_exam_grading import (
    SyntheticScoreProvider,
    auth_api,
    base,
    consume,
    exam,
    marking,
    papers,
    parse_context,
    roster,
    scan_context,
    script_context,
    start,
)

from huipi_cloud.modules.exam_grading import service as g
from huipi_cloud.modules.exam_grading.runtime import ScoringWorker
from huipi_cloud.modules.exam_results import access as result_access
from huipi_cloud.modules.exam_results import service as results
from huipi_cloud.modules.identity.models import User
from huipi_cloud.modules.identity.protocol import Principal
from huipi_cloud.modules.marking_tasks import service as tasks
from huipi_cloud.modules.marking_tasks.models import MarkingOperation, MarkingTask
from huipi_cloud.modules.marking_tasks.permissions import MANAGER_ROLE, TEACHER_ROLE
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityGrant, SecurityScope


@pytest.fixture
async def collaborative(marking, postgres_engine):
    c = marking
    c.postgres_engine = postgres_engine
    async with c.factory() as db:
        await tasks.enable_operator(
            db,
            c.users["admin"].id,
            c.users["admin"].id,
            uuid4(),
            "synthetic marking deployment approval",
        )
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    for name in ["marker_a", "marker_b"]:
        async with c.factory() as db, db.begin():
            u = User(
                issuer=c.users["a"].issuer, subject=str(uuid4()), is_active=True, is_admin=False
            )
            db.add(u)
            await db.flush()
            c.users[name] = u
            c.principals[name] = Principal(
                user_id=u.id,
                issuer=u.issuer,
                subject=u.subject,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        async with c.factory() as db:
            grant = await security.grant_role(
                db,
                c.principals["admin"],
                uuid4(),
                user_id=u.id,
                role_version_id=TEACHER_ROLE,
                scope_id=scope,
            )
            setattr(c, name + "_grant", grant)
    async with c.factory() as db:
        c.manager_grant = await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=MANAGER_ROLE,
            scope_id=scope,
        )
    async with c.factory() as db, db.begin():
        u = User(issuer=c.users["a"].issuer, subject=str(uuid4()), is_active=True, is_admin=True)
        db.add(u)
        await db.flush()
        c.users["plain_admin"] = u
    try:
        yield c
    finally:
        async with c.factory() as db, db.begin():
            await db.execute(
                text("TRUNCATE marking_operations,marking_tasks,exam_result_class_access CASCADE")
            )


async def http(c, path, body=None, actor="a"):
    call = c.client.get if body is None else c.client.post
    return await call(
        "/api/v1" + path, headers=c.headers(actor), **({"json": body} if body is not None else {})
    )


async def assign(c, wid=None, actor="marker_a", previous=None, **extra):
    return await http(
        c,
        f"/exam-sessions/{c.session['id']}/marking-assignments",
        dict(
            request_id=str(uuid4()),
            assignee_user_id=str(c.users[actor].id),
            targets=[
                dict(
                    workspace_id=str(wid or c.wid),
                    expected_task_id=previous["task_id"] if previous else None,
                    expected_revision=previous["revision"] if previous else 0,
                )
            ],
            reason="合成单评任务分配",
            **extra,
        ),
    )


async def task(c, tid, actor="marker_a"):
    r = await http(c, f"/marking-tasks/{tid}", actor=actor)
    assert r.status_code == 200, r.text
    return r.json()


async def claim(c, tid, actor="marker_a", body=None):
    return await http(
        c,
        f"/marking-tasks/{tid}/claim",
        body or dict(request_id=str(uuid4()), expected_revision=1),
        actor,
    )


async def score_review(c, tid, actor="marker_a", status="accepted", *, queue_only=False):
    t = await task(c, tid, actor)
    wid = t["workspace_id"]
    d = (await http(c, f"/exam-grading-workspaces/{wid}", actor=actor)).json()
    assert "nodes" in d, d
    node = (
        next(
            n
            for n in d["nodes"]
            if "Student" in n["text"] or "student" in n["text"] or "2+2" in n["text"]
        )
        if d["nodes"]
        else None
    )
    node = node or d["nodes"][0]
    r = await http(
        c,
        f"/exam-grading-workspaces/{wid}/answer-review",
        dict(
            request_id=str(uuid4()),
            manual_attribution=True,
            expected_revision=d["answer_review"]["revision"] if d["answer_review"] else 0,
            payload=dict(
                decision="response_present",
                response_regions=[
                    dict(
                        content_pointer=node["content_pointer"],
                        text_start=0,
                        text_end=len(node["text"]),
                    )
                ],
                reason_codes=["student_work_visible"],
                reviewer_ref="teacher_" + c.users[actor].id.hex,
            ),
        ),
        actor,
    )
    assert r.status_code == 200, r.text
    d = (await http(c, f"/exam-grading-workspaces/{wid}", actor=actor)).json()
    r = await http(
        c,
        f"/exam-grading-workspaces/{wid}/scoring",
        dict(request_id=str(uuid4()), expected_answer_revision=d["answer_review"]["revision"]),
        actor,
    )
    assert r.status_code == 202, r.text
    if queue_only:
        return r.json()
    assert await c.grade_worker.once()
    d = (await http(c, f"/exam-grading-workspaces/{wid}", actor=actor)).json()
    assert d["scoring_status"] == "succeeded", d
    r = await http(
        c,
        f"/exam-grading-workspaces/{wid}/score-review",
        dict(
            request_id=str(uuid4()),
            expected_revision=d["history"][-1]["review_revision"] if d["history"] else 0,
            payload=dict(
                schema_name="huipi.grading.review",
                schema_version="1.0",
                review_status=status,
                reviewer_ref="teacher_" + c.users[actor].id.hex,
                criterion_reviews=[],
                review_reason_codes=[] if status == "accepted" else ["manual_follow_up_needed"],
            ),
        ),
        actor,
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.anyio
async def test_real_single_marker_claim_review_submission_and_result_gate(collaborative):
    c = collaborative
    r = await assign(c)
    assert r.status_code == 200, r.text
    tid = r.json()["task_ids"][0]
    # New task role cannot browse any unassigned question, old scan source or all students.
    for path in [
        f"/exam-grading-workspaces/{c.wid}",
        f"/exam-parsing-jobs/{c.jid}/grading-questions",
        "/organization/students",
    ]:
        assert (await http(c, path, actor="marker_b")).status_code in {403, 404, 409}
    r = await claim(c, tid)
    assert r.status_code == 200, r.text
    await score_review(c, tid)
    t = await task(c, tid)
    assert t["status"] == "in_progress"
    async with c.factory() as db, db.begin():
        review = await db.scalar(select(g.GR.c.review_id))
        assert not await tasks.access.result_valid(db, UUID(c.wid), review)
    body = dict(request_id=str(uuid4()), expected_revision=2)
    r = await http(c, f"/marking-tasks/{tid}/complete", body, "marker_a")
    assert r.status_code == 200, r.text
    assert (await http(c, f"/marking-tasks/{tid}/complete", body, "marker_a")).json() == r.json()
    assert (await task(c, tid))["status"] == "completed"
    async with c.factory() as db, db.begin():
        assert await tasks.access.result_valid(db, UUID(c.wid), review)
    # No second effective review through the old HTTP path after completion.
    d = await http(c, f"/exam-grading-workspaces/{c.wid}", actor="marker_a")
    assert d.status_code == 200
    assert (await claim(c, tid)).status_code == 409
    progress = await http(c, f"/exam-sessions/{c.session['id']}/marking-progress")
    assert progress.status_code == 200 and progress.json()["counts"][0]["status"] == "completed"
    # Reassigning a completed question immediately removes its old review from effective results.
    old = await task(c, tid)
    r = await assign(c, actor="marker_b", previous=old, force=True)
    assert r.status_code == 200, r.text
    async with c.factory() as db, db.begin():
        assert not await tasks.access.result_valid(db, UUID(c.wid), review)
        assert await db.scalar(select(g.GR.c.review_id).where(g.GR.c.review_id == review)) == review
    assert (await http(c, f"/exam-grading-workspaces/{c.wid}", actor="marker_a")).status_code == 404


@pytest.mark.anyio
async def test_double_claim_and_reassignment_invalidate_old_teacher_and_replay(collaborative):
    c = collaborative
    tid = (await assign(c)).json()["task_ids"][0]
    body = dict(request_id=str(uuid4()), expected_revision=1)
    responses = await asyncio.gather(claim(c, tid, body=body), claim(c, tid))
    assert sorted(r.status_code for r in responses) == [200, 409]
    t = await task(c, tid)
    assert (await assign(c, actor="marker_b", previous=t)).status_code == 409
    r = await assign(c, actor="marker_b", previous=t, force=True)
    assert r.status_code == 200, r.text
    second = r.json()["task_ids"][0]
    assert (await claim(c, tid, body=body)).status_code == 404
    assert (await http(c, f"/exam-grading-workspaces/{c.wid}", actor="marker_a")).status_code in {
        403,
        404,
    }
    assert (await claim(c, second, actor="marker_b")).status_code == 200
    async with c.factory() as db:
        old = await db.get(MarkingTask, UUID(tid))
        assert not old.is_current and old.status == "superseded"
        assert (
            await db.scalar(
                select(func.count()).select_from(MarkingTask).where(MarkingTask.is_current)
            )
            == 1
        )


@pytest.mark.anyio
async def test_revoke_teacher_denies_new_operations_and_idempotent_replay(collaborative):
    c = collaborative
    tid = (await assign(c)).json()["task_ids"][0]
    body = dict(request_id=str(uuid4()), expected_revision=1)
    assert (await claim(c, tid, body=body)).status_code == 200
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.marker_a_grant["grant_id"])
        )
    assert (await claim(c, tid, body=body)).status_code == 404
    assert (await http(c, f"/exam-grading-workspaces/{c.wid}", actor="marker_a")).status_code in {
        403,
        404,
    }
    assert (await http(c, "/my-marking-tasks", actor="marker_a")).json()["items"] == []


@pytest.mark.anyio
async def test_manager_scope_and_client_role_do_not_delegate_student_or_grant_access(collaborative):
    c = collaborative
    for actor in ["marker_a", "marker_b", "plain_admin"]:
        r = await http(c, f"/exam-sessions/{c.session['id']}/marking-pool", actor=actor)
        assert r.status_code == 404, r.text
    r = await c.client.get(
        f"/api/v1/exam-sessions/{c.session['id']}/marking-pool",
        headers=c.headers("marker_a", role="admin", is_admin=True),
    )
    assert r.status_code == 404 and "student_name" not in r.text
    r = await http(
        c,
        f"/exam-sessions/{c.session['id']}/marking-assignments",
        dict(
            request_id=str(uuid4()),
            assignee_user_id=str(c.users["b"].id),
            targets=[dict(workspace_id=c.wid)],
            reason="未授权教师",
        ),
    )
    assert r.status_code == 404
    assert (await http(c, f"/exam-sessions/{uuid4()}/marking-pool")).status_code in {404, 422}


@pytest.mark.anyio
async def test_task_history_direct_sql_and_unsubmitted_missing_review_never_zero(collaborative):
    c = collaborative
    tid = (await assign(c)).json()["task_ids"][0]
    assert (await claim(c, tid)).status_code == 200
    r = await http(
        c,
        f"/marking-tasks/{tid}/complete",
        dict(request_id=str(uuid4()), expected_revision=2),
        "marker_a",
    )
    assert r.status_code == 409
    for statement in [
        text("DELETE FROM marking_tasks WHERE id=:id").bindparams(id=UUID(tid)),
        update(MarkingTask)
        .where(MarkingTask.id == UUID(tid))
        .values(assignee_user_id=c.users["marker_b"].id, revision=3),
        update(MarkingTask)
        .where(MarkingTask.id == UUID(tid))
        .values(status="completed", revision=3, completed_at=datetime.now(UTC)),
    ]:
        async with c.factory() as db:
            with pytest.raises(DBAPIError):
                await db.execute(statement)
            await db.rollback()


@pytest.mark.anyio
async def test_assignment_request_replay_conflict_audit_rollback_and_unknown_commit(
    collaborative, monkeypatch
):
    c = collaborative
    body = dict(
        request_id=str(uuid4()),
        assignee_user_id=str(c.users["marker_a"].id),
        targets=[dict(workspace_id=c.wid)],
        reason="审计与重放合成",
    )

    async def call():
        return await http(c, f"/exam-sessions/{c.session['id']}/marking-assignments", body)

    def fail(connection, cursor, statement, *args):
        if statement.startswith("INSERT INTO marking_operations"):
            raise OperationalError("synthetic audit", {}, Exception("unavailable"))

    event.listen(c.postgres_engine.sync_engine, "before_cursor_execute", fail)
    try:
        assert (await call()).status_code == 503
    finally:
        event.remove(c.postgres_engine.sync_engine, "before_cursor_execute", fail)
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(MarkingTask)) == 0
    r = await call()
    assert r.status_code == 200, r.text
    assert (await call()).json() == r.json()
    assert (
        await http(
            c,
            f"/exam-sessions/{c.session['id']}/marking-assignments",
            dict(body, reason="不同正文"),
        )
    ).status_code == 409
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(MarkingTask)) == 1


@pytest.mark.anyio
async def test_unknown_commit_retry_preserves_one_assignment(collaborative, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    c = collaborative
    original = AsyncSession.commit
    failed = False

    async def lost(db):
        nonlocal failed
        await original(db)
        if not failed:
            failed = True
            raise OperationalError("synthetic commit acknowledgement", {}, Exception("lost"))

    body = dict(
        request_id=str(uuid4()),
        assignee_user_id=str(c.users["marker_a"].id),
        targets=[dict(workspace_id=c.wid)],
        reason="合成未知提交恢复",
    )
    monkeypatch.setattr(AsyncSession, "commit", lost)
    r = await http(c, f"/exam-sessions/{c.session['id']}/marking-assignments", body)
    assert r.status_code == 503, r.text
    monkeypatch.setattr(AsyncSession, "commit", original)
    r = await http(c, f"/exam-sessions/{c.session['id']}/marking-assignments", body)
    assert r.status_code == 200, r.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(MarkingTask)) == 1
        assert await db.scalar(select(func.count()).select_from(MarkingOperation)) == 1


@pytest.mark.anyio
async def test_completed_task_source_retirement_no_longer_effective(collaborative):
    from test_paper_versions import write

    from huipi_cloud.modules.papers.service import retire_paper

    c = collaborative
    tid = (await assign(c)).json()["task_ids"][0]
    assert (await claim(c, tid)).status_code == 200
    await score_review(c, tid)
    r = await http(
        c,
        f"/marking-tasks/{tid}/complete",
        dict(request_id=str(uuid4()), expected_revision=2),
        "marker_a",
    )
    assert r.status_code == 200, r.text
    async with c.factory() as db:
        await retire_paper(db, c.principals["admin"], write(c.paper))
    r = await http(c, f"/exam-grading-workspaces/{c.wid}", actor="marker_a")
    assert r.status_code == 409, r.text
    assert (await http(c, f"/marking-tasks/{tid}", actor="marker_a")).json()[
        "status"
    ] == "invalidated"


@pytest.mark.anyio
async def test_parallel_assignment_has_one_winner_and_guarded_downgrade(
    collaborative, postgres_engine
):
    from test_organization_migration import alembic

    c = collaborative
    responses = await asyncio.gather(assign(c, actor="marker_a"), assign(c, actor="marker_b"))
    assert sorted(r.status_code for r in responses) == [200, 409]
    async with c.factory() as db:
        before = (await db.execute(select(tasks.T))).mappings().all()
    result = await alembic(postgres_engine.url, "downgrade", "b15b71d5f016", success=False)
    assert result.returncode != 0 and "marking_facts_prevent_downgrade" in result.stderr
    async with c.factory() as db:
        assert (await db.execute(select(tasks.T))).mappings().all() == before
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "b16a82e6a017"


@pytest.mark.anyio
async def test_request_id_cannot_be_reused_for_a_different_task_or_oversized_body(collaborative):
    c = collaborative
    async with c.factory() as db:
        other = await db.scalar(select(g.W.c.id).where(g.W.c.id != UUID(c.wid)))
    first = (await assign(c)).json()["task_ids"][0]
    second = (await assign(c, wid=other)).json()["task_ids"][0]
    body = dict(request_id=str(uuid4()), expected_revision=1)
    assert (await claim(c, first, body=body)).status_code == 200
    r = await claim(c, second, body=body)
    assert r.status_code == 409 and r.json()["detail"] == "marking_request_conflict", r.text
    assert (await task(c, second))["status"] == "assigned"
    r = await c.client.post(
        f"/api/v1/marking-tasks/{second}/claim",
        headers={**c.headers("marker_a"), "Content-Type": "application/json"},
        content=b" " * 65537,
    )
    assert r.status_code == 413, r.text
    assert (await http(c, "/marking-tasks/not-a-uuid", actor="marker_a")).status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["reassign", "revoke"])
async def test_late_model_result_after_task_change_cannot_register_proposal(collaborative, change):
    c = collaborative
    tid = (await assign(c)).json()["task_ids"][0]
    assert (await claim(c, tid)).status_code == 200
    queued = await score_review(c, tid, queue_only=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.grade_worker.provider.complete

    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    c.grade_worker.provider.complete = blocked
    running = asyncio.create_task(c.grade_worker.once())
    await asyncio.wait_for(entered.wait(), 15)
    try:
        if change == "reassign":
            old = await task(c, tid)
            r = await assign(c, actor="marker_b", previous=old, force=True)
            assert r.status_code == 200, r.text
        else:
            async with c.factory() as db:
                await security.revoke_grant(
                    db, c.principals["admin"], uuid4(), grant_id=UUID(c.marker_a_grant["grant_id"])
                )
    finally:
        release.set()
        await asyncio.wait_for(running, 20)
    async with c.factory() as db:
        job = await g.row(db, g.S, UUID(queued["scoring_job_id"]))
        assert job["status"] == "invalidated" and job["proposal_id"] is None
        assert await db.scalar(select(func.count()).select_from(g.GP)) == 0
    assert (await http(c, f"/exam-grading-workspaces/{c.wid}", actor="marker_a")).status_code == 404
