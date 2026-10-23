"""Real PG proof for scoped snapshots, database immutability and concurrency."""

import asyncio
import hashlib
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from test_teacher_authorization import auth_api as auth_api

from huipi_cloud.modules.assignments.models import AnswerKey, Assignment, Question, RubricCriterion
from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.papers import service as paper
from huipi_cloud.modules.papers.models import (
    PaperFamily,
    PaperOperation,
    PaperQuestionMapping,
    PaperVersion,
)
from huipi_cloud.modules.papers.permissions import (
    PAPER_LOCKER_ROLE,
    PAPER_READER_ROLE,
)
from huipi_cloud.modules.papers.schemas import QuestionEdit, Reorder, SnapshotCreate, VersionWrite
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityGrant, SecurityScope


@pytest.fixture
async def papers(auth_api):
    from datetime import UTC, datetime, timedelta

    ctx = auth_api
    ctx.principals = {
        name: Principal(
            user_id=u.id,
            issuer=u.issuer,
            subject=u.subject,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            is_admin=u.is_admin,
        )
        for name, u in ctx.users.items()
    }
    async with ctx.factory() as s, s.begin():
        await s.execute(text("TRUNCATE security_audit_events, paper_operations"))
    async with ctx.factory() as s:
        await security.deployment_bootstrap(s, ctx.users["admin"].id, uuid4(), "synthetic approval")
    async with ctx.factory() as s:
        await paper.enable_operator(
            s, ctx.users["admin"].id, ctx.users["admin"].id, uuid4(), "synthetic paper approval"
        )
    ctx.client.headers.update(ctx.headers("a"))
    a = await ctx.client.post(
        "/api/v1/assignments",
        json={"title": "合成原作业", "subject": "数学", "grade_level": "高二"},
    )
    assert a.status_code == 201
    ctx.aid = UUID(a.json()["id"])
    ctx.qids = []
    for number in (1, 2):
        q = await ctx.client.post(
            f"/api/v1/assignments/{ctx.aid}/questions",
            json={
                "question_number": number,
                "question_type": "short_answer",
                "stem": f"合成题{number}：计算2+2",
                "max_score": "10.00",
            },
        )
        assert q.status_code == 201
        qid = UUID(q.json()["id"])
        ctx.qids.append(qid)
        assert (
            await ctx.client.put(
                f"/api/v1/questions/{qid}/answer-key", json={"answer_content": "4"}
            )
        ).status_code == 200
        assert (
            await ctx.client.put(
                f"/api/v1/questions/{qid}/rubric",
                json={
                    "criteria": [
                        {"description": "过程正确", "points": "5.00", "sort_order": 1},
                        {"description": "结果正确", "points": "5.00", "sort_order": 2},
                    ]
                },
            )
        ).status_code == 200
    assert (await ctx.client.post(f"/api/v1/assignments/{ctx.aid}/publish")).status_code == 200
    try:
        yield ctx
    finally:
        async with ctx.factory() as s, s.begin():
            await s.execute(text("TRUNCATE paper_families CASCADE"))
            await s.execute(
                delete(SecurityGrant).where(
                    SecurityGrant.user_id.in_([u.id for u in ctx.users.values()])
                )
            )


async def create(ctx, *, request=None, title="合成共享数学卷"):
    payload = request or SnapshotCreate(
        request_id=uuid4(), source_assignment_id=ctx.aid, title=title
    )
    async with ctx.factory() as s:
        return await paper.create_snapshot(s, ctx.principals["admin"], payload)


async def inspect(ctx, pid, name="admin"):
    async with ctx.factory() as s:
        return await paper.inspect_paper(s, ctx.principals[name], UUID(str(pid)))


def write(summary, **extra):
    return VersionWrite(
        request_id=uuid4(),
        paper_version_id=summary["paper_version_id"],
        expected_revision=summary["revision"],
        expected_content_sha256=summary["content_sha256"],
        expected_mapping_digest=summary["mapping_digest"],
        **extra,
    )


async def lock(ctx, summary, name="admin"):
    async with ctx.factory() as s:
        return await paper.lock_paper(s, ctx.principals[name], write(summary))


def edit(summary, mapping, stem="修改后的合成题"):
    return QuestionEdit(
        **write(summary).model_dump(),
        question_key=mapping["question_key"],
        question={
            "question_number": 1,
            "question_type": "short_answer",
            "stem": stem,
            "max_score": "10.00",
        },
        answer_content="4",
        rubric={"criteria": [{"description": "全部正确", "points": "10.00", "sort_order": 1}]},
    )


async def legacy_digest(ctx):
    async with ctx.factory() as s:
        data = {}
        for model in (Assignment, Question, AnswerKey, RubricCriterion):
            table = model.__table__
            column = (
                table.c.id
                if model is Assignment
                else table.c.assignment_id
                if model is Question
                else table.c.question_id
            )
            values = [ctx.aid] if model in {Assignment, Question} else ctx.qids
            data[table.name] = [
                dict(r)
                for r in (
                    await s.execute(select(table).where(column.in_(values)).order_by(table.c.id))
                ).mappings()
            ]
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


@pytest.mark.anyio
async def test_snapshot_dedup_mapping_provenance_and_legacy_bytes_unchanged(papers):
    ctx = papers
    before = await legacy_digest(ctx)
    request = SnapshotCreate(
        request_id=uuid4(), source_assignment_id=ctx.aid, title="合成共享数学卷"
    )
    first = await create(ctx, request=request)
    assert first == await create(ctx, request=request)
    assert first == await create(ctx)
    assert first["revision"] == 1 and first["version_no"] == 1
    data = await inspect(ctx, first["paper_version_id"])
    assert len(data["mappings"]) == 2 and len(data["content"]["questions"]) == 2
    assert {m["source_question_id"] for m in data["mappings"]} == {str(i) for i in ctx.qids}
    assert all(m["legacy_question_id"] not in {str(i) for i in ctx.qids} for m in data["mappings"])
    async with ctx.factory() as s:
        assert await s.scalar(select(func.count()).select_from(PaperVersion)) == 1
        a = Assignment.__table__
        assert (
            await s.scalar(
                select(a.c.security_classification).where(
                    a.c.id == UUID(first["canonical_assignment_id"])
                )
            )
            == "exam_protected"
        )
        await s.rollback()
        with pytest.raises(AccessError) as error:
            await paper.create_snapshot(
                s, ctx.principals["admin"], request.model_copy(update={"title": "正文不同"})
            )
        assert error.value.code == "paper_request_conflict"
    assert await legacy_digest(ctx) == before
    for name in ("admin", "a"):
        r = await ctx.client.get(
            "/api/v1/assignments/" + first["canonical_assignment_id"], headers=ctx.headers(name)
        )
        assert r.status_code == 404 and r.json() == {"detail": "resource_not_found_or_forbidden"}
        listing = await ctx.client.get("/api/v1/assignments", headers=ctx.headers(name))
        assert first["canonical_assignment_id"] not in listing.text


@pytest.mark.anyio
async def test_authorized_draft_edit_reorder_lock_and_linear_fork(papers):
    ctx = papers
    original = await legacy_digest(ctx)
    version = await create(ctx)
    data = await inspect(ctx, version["paper_version_id"])
    async with ctx.factory() as s:
        version = await paper.edit_question(
            s, ctx.principals["admin"], edit(version, data["mappings"][0])
        )
    assert version["revision"] == 2
    data = await inspect(ctx, version["paper_version_id"])
    async with ctx.factory() as s:
        version = await paper.reorder(
            s,
            ctx.principals["admin"],
            Reorder(
                **write(version).model_dump(),
                mappings=[
                    {"question_key": m["question_key"], "printed_label": f"第{i}题"}
                    for i, m in enumerate(reversed(data["mappings"]), 1)
                ],
            ),
        )
    version = await lock(ctx, version)
    assert version["status"] == "locked" and version["locked_at"]
    async with ctx.factory() as s:
        with pytest.raises(AccessError) as error:
            await paper.edit_question(
                s, ctx.principals["admin"], edit(version, data["mappings"][0])
            )
        assert error.value.code == "paper_locked"
    async with ctx.factory() as s:
        new = await paper.fork_paper(s, ctx.principals["admin"], write(version))
    fork = await inspect(ctx, new["paper_version_id"])
    old = await inspect(ctx, version["paper_version_id"])
    assert new["version_no"] == 2 and new["prior_version_id"] == version["paper_version_id"]
    assert new["canonical_assignment_id"] != version["canonical_assignment_id"]
    assert [m["question_key"] for m in fork["mappings"]] == [
        m["question_key"] for m in old["mappings"]
    ]
    assert {m["prior_mapping_id"] for m in fork["mappings"]} == {m["id"] for m in old["mappings"]}
    assert old["content"]["questions"][0]["stem"] == "修改后的合成题"
    async with ctx.factory() as s:
        await paper.edit_question(
            s, ctx.principals["admin"], edit(new, fork["mappings"][1], stem="新版独立编辑")
        )
    assert (await inspect(ctx, version["paper_version_id"]))["content"] == old["content"]
    assert await legacy_digest(ctx) == original


@pytest.mark.anyio
async def test_locked_core_sql_and_orm_cannot_mutate_any_content_or_mapping(papers):
    ctx = papers
    version = await lock(ctx, await create(ctx))
    data = await inspect(ctx, version["paper_version_id"])
    aid = UUID(version["canonical_assignment_id"])
    qid = UUID(data["mappings"][0]["legacy_question_id"])
    async with ctx.factory() as s:
        kid = await s.scalar(
            select(AnswerKey.__table__.c.id).where(AnswerKey.__table__.c.question_id == qid)
        )
    statements = [
        update(Assignment.__table__).where(Assignment.__table__.c.id == aid).values(title="attack"),
        delete(Assignment.__table__).where(Assignment.__table__.c.id == aid),
        insert(Question.__table__).values(
            id=uuid4(),
            assignment_id=aid,
            question_number=99,
            question_type="essay",
            stem="attack",
            max_score=1,
        ),
        update(Question.__table__).where(Question.__table__.c.id == qid).values(stem="attack"),
        update(Question.__table__)
        .where(Question.__table__.c.id == qid)
        .values(assignment_id=ctx.aid),
        delete(Question.__table__).where(Question.__table__.c.id == qid),
        update(AnswerKey.__table__)
        .where(AnswerKey.__table__.c.id == kid)
        .values(answer_content="attack"),
        delete(AnswerKey.__table__).where(AnswerKey.__table__.c.id == kid),
        insert(AnswerKey.__table__).values(id=uuid4(), question_id=qid, answer_content="attack"),
        update(RubricCriterion.__table__)
        .where(RubricCriterion.__table__.c.question_id == qid)
        .values(points=1),
        delete(RubricCriterion.__table__).where(RubricCriterion.__table__.c.question_id == qid),
        insert(RubricCriterion.__table__).values(
            id=uuid4(), question_id=qid, description="attack", points=1, sort_order=99
        ),
        update(PaperQuestionMapping.__table__)
        .where(
            PaperQuestionMapping.__table__.c.paper_version_id == UUID(version["paper_version_id"])
        )
        .values(printed_label="attack"),
        delete(PaperQuestionMapping.__table__).where(
            PaperQuestionMapping.__table__.c.paper_version_id == UUID(version["paper_version_id"])
        ),
        update(PaperVersion)
        .where(PaperVersion.id == UUID(version["paper_version_id"]))
        .values(status="draft"),
        delete(PaperVersion).where(PaperVersion.id == UUID(version["paper_version_id"])),
        update(PaperVersion)
        .where(PaperVersion.id == UUID(version["paper_version_id"]))
        .values(title="attack"),
        update(PaperVersion)
        .where(PaperVersion.id == UUID(version["paper_version_id"]))
        .values(rubric_digest="b" * 64),
    ]
    for statement in statements:
        async with ctx.factory() as s:
            with pytest.raises(DBAPIError) as error:
                await s.execute(statement)
            assert getattr(error.value.orig, "sqlstate", None) == "23514"
            assert any(
                code in str(error.value.orig)
                for code in ("paper_content_locked", "paper_version_immutable")
            )
            await s.rollback()
    # Load a protected Question via trusted test-only from_statement, then ORM flush;
    # this proves a loaded instance cannot bypass the database trigger.
    async with ctx.factory() as s:
        q = (
            await s.execute(
                select(Question)
                .from_statement(text("SELECT * FROM questions WHERE id=:id"))
                .params(id=qid)
            )
        ).scalar_one()
        q.stem = "ORM attack"
        with pytest.raises(DBAPIError) as error:
            await s.flush()
        assert "paper_content_locked" in str(error.value.orig)
        await s.rollback()
    assert (await inspect(ctx, version["paper_version_id"])) == data


@pytest.mark.anyio
async def test_mapping_composite_fk_and_unknown_scope_reject_cross_carrier(papers):
    ctx = papers
    version = await create(ctx)
    m = PaperQuestionMapping.__table__
    async with ctx.factory() as s:
        with pytest.raises(DBAPIError):
            await s.execute(
                insert(m).values(
                    id=uuid4(),
                    paper_version_id=UUID(version["paper_version_id"]),
                    canonical_assignment_id=UUID(version["canonical_assignment_id"]),
                    question_key=uuid4(),
                    legacy_question_id=ctx.qids[0],
                    printed_label="bad",
                    sort_order=9,
                    source_question_id=ctx.qids[0],
                    source_rubric_sha256="a" * 64,
                )
            )
        await s.rollback()
        with pytest.raises(DBAPIError):
            await s.execute(
                insert(SecurityScope).values(
                    id=uuid4(), kind="paper_version", paper_version_id=uuid4()
                )
            )
        await s.rollback()


@pytest.mark.anyio
async def test_real_scoped_read_edit_lock_revocation_and_legacy_admin_denial(papers):
    ctx = papers
    v = await create(ctx)
    pid = UUID(v["paper_version_id"])
    for name in ("a", "b"):
        with pytest.raises(AccessError):
            await inspect(ctx, pid, name)
    async with ctx.factory() as s, s.begin():
        # Existing admin flag is irrelevant after explicit paper grants are removed.
        await s.execute(
            update(SecurityGrant)
            .where(
                SecurityGrant.user_id == ctx.users["admin"].id,
                SecurityGrant.role_version_id == paper.PAPER_OPERATOR_ROLE,
            )
            .values(revoked_at=func.clock_timestamp())
        )
    with pytest.raises(AccessError):
        await inspect(ctx, pid, "admin")
    # Use the separate trusted deployment operation again (not bearer self-elevation).
    async with ctx.factory() as s:
        await paper.enable_operator(
            s, ctx.users["admin"].id, ctx.users["admin"].id, uuid4(), "synthetic regrant approval"
        )
    async with ctx.factory() as s:
        scope_id = await s.scalar(
            select(SecurityScope.id).where(SecurityScope.paper_version_id == pid)
        )
    grants = []
    for role in (PAPER_READER_ROLE, PAPER_LOCKER_ROLE):
        async with ctx.factory() as s:
            grants.append(
                await security.grant_role(
                    s,
                    ctx.principals["admin"],
                    uuid4(),
                    user_id=ctx.users["a"].id,
                    role_version_id=role,
                    scope_id=scope_id,
                )
            )
    assert (await inspect(ctx, pid, "a"))["paper_version_id"] == str(pid)
    async with ctx.factory() as s:
        await security.revoke_grant(
            s, ctx.principals["admin"], uuid4(), grant_id=UUID(grants[1]["grant_id"])
        )
    with pytest.raises(AccessError):
        await lock(ctx, v, "a")
    assert (await inspect(ctx, pid))["status"] == "draft"


@pytest.mark.anyio
async def test_different_request_concurrent_snapshot_creates_one_carrier(papers):
    ctx = papers
    barrier = asyncio.Barrier(2)

    async def invoke():
        await barrier.wait()
        return await create(ctx)

    one, two = await asyncio.gather(invoke(), invoke())
    assert one == two
    async with ctx.factory() as s:
        assert await s.scalar(select(func.count()).select_from(PaperVersion)) == 1
        assert await s.scalar(select(func.count()).select_from(PaperQuestionMapping)) == 2


@pytest.mark.anyio
async def test_lock_and_edit_same_revision_serialize_without_partial_result(papers):
    ctx = papers
    v = await create(ctx)
    data = await inspect(ctx, v["paper_version_id"])
    barrier = asyncio.Barrier(2)

    async def editing():
        await barrier.wait()
        try:
            async with ctx.factory() as s:
                return await paper.edit_question(
                    s, ctx.principals["admin"], edit(v, data["mappings"][0])
                )
        except AccessError as e:
            return e.code

    async def locking():
        await barrier.wait()
        try:
            return await lock(ctx, v)
        except AccessError as e:
            return e.code

    results = await asyncio.gather(editing(), locking())
    assert sum(isinstance(r, dict) for r in results) == 1
    failure = next(r for r in results if isinstance(r, str))
    assert failure in {"paper_locked", "paper_revision_conflict"}
    actual = await inspect(ctx, v["paper_version_id"])
    assert actual["revision"] == 2
    assert actual["content"]["questions"][0]["stem"] == (
        "合成题1：计算2+2" if actual["status"] == "locked" else "修改后的合成题"
    )


@pytest.mark.anyio
async def test_audit_failure_and_unknown_commit_are_atomic_and_recoverable(
    papers, postgres_engine, monkeypatch
):
    ctx = papers
    before = await legacy_digest(ctx)

    def fail(conn, cursor, statement, params, context, many):
        if statement.startswith("INSERT INTO paper_operations"):
            raise OperationalError("synthetic audit", {}, Exception("synthetic unavailable"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(AccessError) as e:
            await create(ctx)
        assert e.value.code == "paper_operation_unavailable"
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail)
    async with ctx.factory() as s:
        assert await s.scalar(select(func.count()).select_from(PaperVersion)) == 0
        assert await s.scalar(select(func.count()).select_from(PaperFamily)) == 0
    commit = ctx.factory.class_.commit

    async def lost_ack(s):
        await commit(s)
        if s.info.pop("lost_ack", False):
            raise OSError("synthetic lost confirmation")

    monkeypatch.setattr(ctx.factory.class_, "commit", lost_ack)
    req = SnapshotCreate(request_id=uuid4(), source_assignment_id=ctx.aid, title="合成未知提交")
    async with ctx.factory() as s:
        s.info["lost_ack"] = True
        with pytest.raises(AccessError) as e:
            await paper.create_snapshot(s, ctx.principals["admin"], req)
        assert e.value.code == "paper_commit_outcome_unknown"
    v = await create(ctx, request=req)
    async with ctx.factory() as s:
        assert await s.scalar(select(func.count()).select_from(PaperVersion)) == 1
        assert (
            await s.scalar(
                select(func.count())
                .select_from(PaperOperation)
                .where(PaperOperation.request_id == req.request_id)
            )
            == 1
        )
    assert v["status"] == "draft" and await legacy_digest(ctx) == before


async def wait_for_pg_lock(factory, fragment):
    async with factory() as s, asyncio.timeout(5):
        while True:
            await s.execute(text("SELECT pg_stat_clear_snapshot()"))
            waiting = await s.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                    "AND wait_event_type='Lock' AND query LIKE :pattern)"
                ),
                dict(pattern="%" + fragment + "%"),
            )
            if waiting:
                return
            await asyncio.sleep(0)  # Wait on observed PG lock state, not timing luck.


@pytest.mark.anyio
async def test_direct_writer_before_lock_is_observed_as_changed_source(papers):
    ctx = papers
    v = await create(ctx)
    data = await inspect(ctx, v["paper_version_id"])
    qid = UUID(data["mappings"][0]["legacy_question_id"])
    held = asyncio.Event()
    release = asyncio.Event()

    async def writer():
        async with ctx.factory() as s, s.begin():
            await s.execute(
                update(Question.__table__)
                .where(Question.__table__.c.id == qid)
                .values(stem="直接草稿编辑")
            )
            held.set()
            await release.wait()

    writing = asyncio.create_task(writer())
    await held.wait()
    locking = asyncio.create_task(lock(ctx, v))
    try:
        await wait_for_pg_lock(ctx.factory, "paper_versions")
    finally:
        release.set()
        await writing
    with pytest.raises(AccessError) as e:
        await locking
    assert e.value.code == "paper_content_changed"
    current = await inspect(ctx, v["paper_version_id"])
    assert current["status"] == "draft"
    assert current["content"]["questions"][0]["stem"] == "直接草稿编辑"
    assert (await lock(ctx, current))["status"] == "locked"


@pytest.mark.anyio
async def test_lock_before_direct_writer_makes_database_reject_late_write(papers, monkeypatch):
    ctx = papers
    v = await create(ctx)
    data = await inspect(ctx, v["paper_version_id"])
    qid = UUID(data["mappings"][0]["legacy_question_id"])
    held = asyncio.Event()
    release = asyncio.Event()
    bump = paper.bump

    async def pause(s, p, **changes):
        if changes.get("status") == "locked":
            held.set()
            await release.wait()
        return await bump(s, p, **changes)

    monkeypatch.setattr(paper, "bump", pause)
    locking = asyncio.create_task(lock(ctx, v))
    await held.wait()

    async def writer():
        async with ctx.factory() as s, s.begin():
            await s.execute(
                update(Question.__table__)
                .where(Question.__table__.c.id == qid)
                .values(stem="绝不能写入")
            )

    writing = asyncio.create_task(writer())
    try:
        await wait_for_pg_lock(ctx.factory, "UPDATE questions")
    finally:
        release.set()
    assert (await locking)["status"] == "locked"
    with pytest.raises(DBAPIError):
        await writing
    assert (await inspect(ctx, v["paper_version_id"]))["content"] == data["content"]


@pytest.mark.anyio
async def test_source_snapshot_waits_for_complete_legacy_content_transaction(papers):
    ctx = papers
    held = asyncio.Event()
    release = asyncio.Event()

    async def writer():
        async with ctx.factory() as s, s.begin():
            await s.execute(
                select(Assignment.__table__.c.id)
                .where(Assignment.__table__.c.id == ctx.aid)
                .with_for_update()
            )
            await s.execute(
                update(Question.__table__)
                .where(Question.__table__.c.id == ctx.qids[0])
                .values(stem="新来源题干")
            )
            await s.execute(
                update(RubricCriterion.__table__)
                .where(
                    RubricCriterion.__table__.c.question_id == ctx.qids[0],
                    RubricCriterion.__table__.c.sort_order == 1,
                )
                .values(description="新来源评分标准")
            )
            held.set()
            await release.wait()

    writing = asyncio.create_task(writer())
    await held.wait()
    creating = asyncio.create_task(create(ctx))
    try:
        await wait_for_pg_lock(ctx.factory, "assignments")
    finally:
        release.set()
        await writing
    v = await creating
    data = await inspect(ctx, v["paper_version_id"])
    assert data["content"]["questions"][0]["stem"] == "新来源题干"
    assert data["content"]["questions"][0]["rubric"][0]["description"] == "新来源评分标准"
    assert v["source_content_sha256"] == v["content_sha256"]


@pytest.mark.anyio
async def test_permission_revoke_waits_for_inflight_lock_then_next_request_denied(
    papers, monkeypatch
):
    ctx = papers
    v = await create(ctx)
    pid = UUID(v["paper_version_id"])
    async with ctx.factory() as s:
        scope = await s.scalar(
            select(SecurityScope.id).where(SecurityScope.paper_version_id == pid)
        )
    async with ctx.factory() as s:
        grant = await security.grant_role(
            s,
            ctx.principals["admin"],
            uuid4(),
            user_id=ctx.users["a"].id,
            role_version_id=PAPER_LOCKER_ROLE,
            scope_id=scope,
        )
    held = asyncio.Event()
    release = asyncio.Event()
    original = paper.validate_write

    async def pause(s, request, **kw):
        result = await original(s, request, **kw)
        held.set()
        await release.wait()
        return result

    monkeypatch.setattr(paper, "validate_write", pause)
    locking = asyncio.create_task(lock(ctx, v, "a"))
    await held.wait()

    async def revoke():
        async with ctx.factory() as s:
            return await security.revoke_grant(
                s, ctx.principals["admin"], uuid4(), grant_id=UUID(grant["grant_id"])
            )

    revoking = asyncio.create_task(revoke())
    try:
        await wait_for_pg_lock(ctx.factory, "security_grants")
    finally:
        release.set()
    assert (await locking)["status"] == "locked"
    assert not (await revoking)["active"]
    with pytest.raises(AccessError):
        await lock(ctx, v, "a")


@pytest.mark.anyio
async def test_source_authority_and_paper_scopes_cannot_be_combined_across_resources(papers):
    from huipi_cloud.modules.papers.permissions import PAPER_EDITOR_ROLE, PAPER_OPERATOR_ROLE

    ctx = papers
    v = await create(ctx)
    other = await create(ctx, title="另一份合成卷")
    async with ctx.factory() as s:
        target = await s.scalar(
            select(SecurityScope.id).where(
                SecurityScope.paper_version_id == UUID(v["paper_version_id"])
            )
        )
    async with ctx.factory() as s:
        await security.grant_role(
            s,
            ctx.principals["admin"],
            uuid4(),
            user_id=ctx.users["a"].id,
            role_version_id=PAPER_EDITOR_ROLE,
            scope_id=target,
        )
    assert (await inspect(ctx, v["paper_version_id"], "a"))["title"] == v["title"]
    with pytest.raises(AccessError):
        await inspect(ctx, other["paper_version_id"], "a")
    async with ctx.factory() as s:
        await security.grant_role(
            s,
            ctx.principals["admin"],
            uuid4(),
            user_id=ctx.users["b"].id,
            role_version_id=PAPER_OPERATOR_ROLE,
            scope_id=security.INSTALLATION_SCOPE,
        )
    # Even a real paper-operator Grant cannot borrow an unreadable legacy source.
    async with ctx.factory() as s:
        with pytest.raises(AccessError):
            await paper.create_snapshot(
                s,
                ctx.principals["b"],
                SnapshotCreate(request_id=uuid4(), source_assignment_id=ctx.aid, title="不能复制"),
            )
    async with ctx.factory() as s:
        assert await s.scalar(select(func.count()).select_from(PaperVersion)) == 2


@pytest.mark.anyio
async def test_retirement_preserves_frozen_bytes_and_replay_still_checks_authority(papers):
    ctx = papers
    v = await lock(ctx, await create(ctx))
    old = await inspect(ctx, v["paper_version_id"])
    request = write(v)
    async with ctx.factory() as s:
        r = await paper.retire_paper(s, ctx.principals["admin"], request)
    assert r["status"] == "retired" and r["locked_at"] == v["locked_at"]
    assert (await inspect(ctx, v["paper_version_id"]))["content"] == old["content"]
    async with ctx.factory() as s:
        assert await paper.retire_paper(s, ctx.principals["admin"], request) == r
    async with ctx.factory() as s, s.begin():
        await s.execute(
            update(SecurityGrant)
            .where(
                SecurityGrant.user_id == ctx.users["admin"].id,
                SecurityGrant.role_version_id == paper.PAPER_OPERATOR_ROLE,
            )
            .values(revoked_at=func.clock_timestamp())
        )
    async with ctx.factory() as s:
        with pytest.raises(AccessError):
            await paper.retire_paper(s, ctx.principals["admin"], request)


@pytest.mark.anyio
async def test_legacy_submission_proposal_and_review_history_survive_snapshot(
    papers, postgres_engine
):
    from test_grade_review import _make_candidate, _review_payload

    from huipi_cloud.infrastructure.database.base import Base
    from huipi_cloud.modules.grading.grade_review_service import record_grade_review

    ctx = papers
    factory, sid, q, _, _, _, proposal, *_ = await _make_candidate(
        ctx.client, ctx.storage, postgres_engine
    )
    await record_grade_review(
        factory,
        ctx.storage,
        proposal_id=proposal.proposal_id,
        request_id=uuid4(),
        expected_revision=0,
        payload=_review_payload(),
    )
    before_file = await ctx.client.get(f"/api/v1/submissions/{sid}/file")
    assert before_file.status_code == 200

    async def history_digest():
        values = {}
        async with factory() as s:
            for mapper in Base.registry.mappers:
                name = mapper.local_table.name
                if name.startswith(("paper_", "security_", "permission_")):
                    continue
                rows = (
                    (
                        await s.execute(
                            select(*[getattr(mapper.class_, c.name) for c in mapper.columns])
                        )
                    )
                    .mappings()
                    .all()
                )
                values[name] = sorted([dict(r) for r in rows], key=lambda r: str(r.get("id")))
        return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()

    before = await history_digest()
    async with factory() as s:
        v = await paper.create_snapshot(
            s,
            ctx.principals["admin"],
            SnapshotCreate(
                request_id=uuid4(), source_assignment_id=q.assignment_id, title="合成历史兼容卷"
            ),
        )
    await lock(ctx, v)
    assert await history_digest() == before
    after_file = await ctx.client.get(f"/api/v1/submissions/{sid}/file")
    assert after_file.content == before_file.content


@pytest.mark.anyio
async def test_paper_migration_roundtrip_preserves_actual_old_graph_and_guards_new_data(
    papers, postgres_engine
):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from sqlalchemy.ext.asyncio import create_async_engine

    from huipi_cloud.infrastructure.database.base import Base

    ctx = papers
    source = {}
    async with ctx.factory() as s:
        for table in Base.metadata.sorted_tables:
            if (
                table.name.startswith(
                    (
                        "paper_",
                        "security_",
                        "permission_",
                        "exam_",
                        "scan_",
                        "exam_parsing_", "exam_submission_links", "answer_script",
                        "current_page_allocations",
                    )
                )
                or table.name in {"session_papers", "exam_participations"}
                or table.name.startswith("roster_")
            ):
                continue
            source[table.name] = [dict(r) for r in (await s.execute(select(table))).mappings()]

    def fingerprint(rows):
        return hashlib.sha256(
            json.dumps(
                sorted(rows, key=lambda r: str(r.get("id"))), sort_keys=True, default=str
            ).encode()
        ).hexdigest()

    expected = {name: fingerprint(rows) for name, rows in source.items()}
    name = "huipi_paper_roundtrip_" + uuid4().hex[:10] + "_test"
    url = postgres_engine.url.set(database=name)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    target = create_async_engine(url)
    env = dict(os.environ, DATABASE_URL=url.render_as_string(hide_password=False))

    async def alembic(*args, success=True):
        r = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", *args],
            env=env,
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
        )
        if success:
            assert r.returncode == 0, r.stderr[-1500:]
        return r

    try:
        async with admin.connect() as c:
            await c.execute(text('CREATE DATABASE "' + name + '"'))
        await alembic("upgrade", "b11b0a7c4e21")
        async with target.begin() as c:
            for table in Base.metadata.sorted_tables:
                if table.name in source and source[table.name]:
                    await c.execute(insert(table), source[table.name])
        for action in [("upgrade", "head"), ("downgrade", "b11b0a7c4e21"), ("upgrade", "head")]:
            await alembic(*action)
            await target.dispose()
            if action[0] == "upgrade":
                await alembic("check")
            async with target.connect() as c:
                for table_name, sha in expected.items():
                    actual = [
                        dict(r)
                        for r in (
                            await c.execute(text('SELECT * FROM "' + table_name + '"'))
                        ).mappings()
                    ]
                    assert fingerprint(actual) == sha, table_name
        async with target.begin() as c:
            triggers = set(
                await c.scalars(text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))
            )
            assert {
                "freeze_paper_content",
                "freeze_paper_version",
                "freeze_paper_mapping",
                "immutable_paper_record",
            } <= triggers
            constraints = set(await c.scalars(text("SELECT conname FROM pg_constraint")))
            assert {
                "fk_mapping_question_carrier",
                "fk_mapping_paper_carrier",
                "uq_paper_content_carrier",
            } <= constraints
            await c.execute(
                insert(PaperFamily.__table__).values(
                    id=uuid4(),
                    creation_key_sha256="c" * 64,
                    created_by_user_id=ctx.users["admin"].id,
                )
            )
        rejected = await alembic("downgrade", "b11b0a7c4e21", success=False)
        assert (
            rejected.returncode != 0
            and "paper_downgrade_requires_archived_new_data" in rejected.stderr
        )
        async with target.connect() as c:
            assert await c.scalar(text("SELECT version_num FROM alembic_version")) == "b14b41a2e903"
    finally:
        await target.dispose()
        async with admin.connect() as c:
            await c.execute(text('DROP DATABASE IF EXISTS "' + name + '"'))
        await admin.dispose()


@pytest.mark.anyio
async def test_cli_token_verification_and_forged_role_cannot_authorize_paper(papers, monkeypatch):
    import argparse
    import io
    import sys

    from huipi_cloud.modules.papers import __main__ as cli

    ctx = papers
    v = await create(ctx)
    monkeypatch.setattr(cli, "session_factory", ctx.factory)
    monkeypatch.setattr(cli, "TokenVerifier", lambda config: ctx.verifier)
    args = argparse.Namespace(
        command="inspect", token_stdin=True, paper_version_id=UUID(v["paper_version_id"])
    )
    token = ctx.headers("admin")["Authorization"].split(" ", 1)[1]
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    result = await cli.run(args)
    assert result["content"]["questions"][0]["answer_key"]["answer_content"] == "4"
    token = ctx.headers("a", role="paper_operator", is_admin=True)["Authorization"].split(" ", 1)[1]
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    with pytest.raises(AccessError) as error:
        await cli.run(args)
    assert error.value.code == "resource_not_found_or_forbidden"


@pytest.mark.anyio
async def test_changed_draft_not_silently_reused_and_content_size_rollback(papers, monkeypatch):
    ctx = papers
    v = await create(ctx)
    data = await inspect(ctx, v["paper_version_id"])
    async with ctx.factory() as s:
        await paper.edit_question(s, ctx.principals["admin"], edit(v, data["mappings"][0]))
    with pytest.raises(AccessError) as e:
        await create(ctx)
    assert e.value.code == "paper_draft_exists"
    current = await inspect(ctx, v["paper_version_id"])
    original_budget = paper.MAX_CONTENT_BYTES
    monkeypatch.setattr(paper, "MAX_CONTENT_BYTES", 1)
    async with ctx.factory() as s:
        with pytest.raises(AccessError) as e:
            await paper.edit_question(
                s, ctx.principals["admin"], edit(current, data["mappings"][0], stem="超限不得保存")
            )
        assert e.value.status_code == 413
    monkeypatch.setattr(paper, "MAX_CONTENT_BYTES", original_budget)
    assert (await inspect(ctx, v["paper_version_id"])) == current


@pytest.mark.anyio
async def test_dedicated_carrier_unique_and_legacy_carrier_binding_forbidden(papers):
    ctx = papers
    v = await create(ctx)
    async with ctx.factory() as s:
        original = dict(
            (
                await s.execute(
                    select(PaperVersion.__table__).where(
                        PaperVersion.__table__.c.id == UUID(v["paper_version_id"])
                    )
                )
            )
            .mappings()
            .one()
        )
        await s.rollback()
        family_id = uuid4()
        await s.execute(
            insert(PaperFamily).values(
                id=family_id, creation_key_sha256="e" * 64, created_by_user_id=ctx.users["admin"].id
            )
        )
        data = dict(original, id=uuid4(), paper_family_id=family_id)
        with pytest.raises(DBAPIError) as e:
            await s.execute(insert(PaperVersion.__table__).values(**data))
        assert getattr(e.value.orig, "sqlstate", None) == "23505"
        assert "uq_paper_content_carrier" in str(e.value.orig)
        await s.rollback()
        data = dict(original, id=uuid4(), canonical_assignment_id=ctx.aid)
        with pytest.raises(DBAPIError) as e:
            await s.execute(insert(PaperVersion.__table__).values(**data))
        assert "paper_carrier_invalid" in str(e.value.orig)
        await s.rollback()
    assert (await inspect(ctx, v["paper_version_id"]))["canonical_assignment_id"] == v[
        "canonical_assignment_id"
    ]


@pytest.mark.anyio
async def test_incomplete_source_rolls_back_family_and_never_reclassifies_homework(papers):
    ctx = papers
    async with ctx.factory() as s, s.begin():
        await s.execute(
            update(RubricCriterion.__table__)
            .where(RubricCriterion.__table__.c.question_id == ctx.qids[0])
            .values(points="1.00")
        )
    with pytest.raises(AccessError) as error:
        await create(ctx)
    assert error.value.code == "paper_source_incomplete"
    async with ctx.factory() as s:
        assert await s.scalar(select(func.count()).select_from(PaperVersion)) == 0
        assert await s.scalar(select(func.count()).select_from(PaperFamily)) == 0
        assert (
            await s.scalar(
                select(Assignment.__table__.c.security_classification).where(
                    Assignment.__table__.c.id == ctx.aid
                )
            )
            == "legacy_homework"
        )


@pytest.mark.anyio
async def test_changed_legacy_source_requires_explicit_fork_not_inferred_version(papers):
    ctx = papers
    v = await lock(ctx, await create(ctx))
    before = await inspect(ctx, v["paper_version_id"])
    async with ctx.factory() as s, s.begin():
        await s.execute(
            update(Question.__table__)
            .where(Question.__table__.c.id == ctx.qids[0])
            .values(stem="来源作业后续变化")
        )
    with pytest.raises(AccessError) as error:
        await create(ctx)
    assert error.value.code == "paper_new_version_requires_fork"
    assert (await inspect(ctx, v["paper_version_id"])) == before
    async with ctx.factory() as s:
        fork = await paper.fork_paper(s, ctx.principals["admin"], write(v))
    assert fork["version_no"] == 2
    assert (await inspect(ctx, fork["paper_version_id"]))["content"] == before["content"]
