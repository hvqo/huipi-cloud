"""Independent PostgreSQL exam planning, cross-resource authorization and races."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from test_paper_versions import create, legacy_digest, lock, wait_for_pg_lock, write
from test_paper_versions import papers as papers
from test_teacher_authorization import auth_api as auth_api

from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.exams.models import ExamOperation, ExamSession, SessionPaper
from huipi_cloud.modules.exams.permissions import (
    EXAM_MANAGER_ROLE,
    PAPER_USER_ROLE,
    SESSION_MANAGER_ROLE,
)
from huipi_cloud.modules.exams.schemas import (
    EventCreate,
    EventState,
    PaperBind,
    PaperUnbind,
    SessionCreate,
    SessionEdit,
    SessionState,
)
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.organization.models import AcademicTerm, AcademicYear, Subject
from huipi_cloud.modules.papers import service as paper_service
from huipi_cloud.modules.papers.models import PaperVersion
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import (
    SecurityGrant,
    SecurityRolePermission,
    SecurityRoleVersion,
    SecurityScope,
)


@pytest.fixture
async def exam(papers):
    c = papers
    async with c.factory() as s:
        await exams.enable_operator(
            s, c.users["admin"].id, c.users["admin"].id, uuid4(), "synthetic deployment approval"
        )
    async with c.factory() as s, s.begin():
        y = AcademicYear(
            code="exam-" + uuid4().hex[:16],
            display_name="合成学年",
            start_date=date(2026, 9, 1),
            end_date=date(2027, 7, 1),
        )
        s.add(y)
        await s.flush()
        t = AcademicTerm(
            academic_year_id=y.id,
            term_no=1,
            code="fall",
            start_date=date(2026, 9, 1),
            end_date=date(2027, 1, 31),
        )
        math = Subject(code="math-" + uuid4().hex[:16], display_name="合成数学")
        english = Subject(code="eng-" + uuid4().hex[:16], display_name="合成英语")
        s.add_all([t, math, english])
        await s.flush()
        c.term_id = t.id
        c.subject_ids = [math.id, english.id]
    c.paper = await lock(c, await create(c))
    c.event = await call(
        c,
        exams.create_event,
        EventCreate(
            request_id=uuid4(),
            academic_term_id=c.term_id,
            title="合成高二期中考试",
            category="midterm",
        ),
    )
    try:
        yield c
    finally:
        async with c.factory() as s, s.begin():
            await s.execute(text("TRUNCATE exam_events, exam_operations CASCADE"))
            await s.execute(
                delete(SecurityGrant).where(
                    SecurityGrant.user_id.in_([u.id for u in c.users.values()])
                )
            )


async def call(c, fn, payload, name="admin"):
    async with c.factory() as s:
        return await fn(s, c.principals[name], payload)


async def read_event(c, name="admin"):
    return await call(c, exams.inspect_event, UUID(c.event["id"]), name)


async def new_session(c, *, offset=0, policy="single", subject=0, event_id=None):
    eid = UUID(event_id or c.event["id"])
    e = await call(c, exams.inspect_event, eid)
    return await call(
        c,
        exams.create_session,
        SessionCreate(
            request_id=uuid4(),
            exam_event_id=eid,
            subject_id=c.subject_ids[subject],
            expected_event_revision=e["revision"],
            title="合成科目场次",
            starts_at=datetime(2026, 11, 1, 1, tzinfo=UTC) + timedelta(hours=offset),
            ends_at=datetime(2026, 11, 1, 3, tzinfo=UTC) + timedelta(hours=offset),
            paper_policy=policy,
        ),
    )


def bind_request(c, s, **extra):
    return PaperBind(
        request_id=uuid4(),
        exam_session_id=s["id"],
        expected_revision=s["revision"],
        paper_version_id=c.paper["paper_version_id"],
        **extra,
    )


def state_request(s, state):
    return SessionState(
        request_id=uuid4(),
        exam_session_id=s["id"],
        expected_revision=s["revision"],
        lifecycle=state,
    )


async def grant(c, role, identifier, kind="exam_session", name="a"):
    column = getattr(SecurityScope, kind + "_id")
    async with c.factory() as s:
        scope = await s.scalar(select(SecurityScope.id).where(column == UUID(str(identifier))))
    async with c.factory() as s:
        return await security.grant_role(
            s,
            c.principals["admin"],
            uuid4(),
            user_id=c.users[name].id,
            role_version_id=role,
            scope_id=scope,
        )


@pytest.mark.anyio
async def test_event_multi_subject_reuse_without_carrier_or_class_copy(exam):
    c = exam
    before = await legacy_digest(c)
    first = await new_session(c)
    second = await new_session(c, offset=3, subject=1)
    r1 = await call(c, exams.bind_paper, bind_request(c, first))
    r2 = await call(c, exams.bind_paper, bind_request(c, second))
    assert (
        r1["binding"]["paper_version_id"]
        == r2["binding"]["paper_version_id"]
        == c.paper["paper_version_id"]
    )
    assert (await read_event(c))["session_count"] == 2
    async with c.factory() as s:
        assert await s.scalar(select(func.count()).select_from(PaperVersion)) == 1
        assert await s.scalar(select(func.count()).select_from(ExamSession)) == 2
    assert await legacy_digest(c) == before
    data = await call(c, exams.inspect_session, UUID(first["id"]))
    assert (
        data["roster_status"] == "not_configured" and data["attendance_status"] == "not_available"
    )
    assert "answer_content" not in str(data) and "rubric" not in str(data)
    # A different ExamEvent reuses the same version, not a cloned carrier.
    other = await call(
        c,
        exams.create_event,
        EventCreate(request_id=uuid4(), academic_term_id=c.term_id, title="另一考试"),
    )
    third = await new_session(c, offset=8, event_id=other["id"])
    assert (await call(c, exams.bind_paper, bind_request(c, third)))["binding"][
        "paper_version_id"
    ] == c.paper["paper_version_id"]


@pytest.mark.anyio
async def test_ab_configuration_ready_and_historical_replacement(exam):
    c = exam
    s = await new_session(c, policy="ab")
    first = await call(c, exams.bind_paper, bind_request(c, s))
    with pytest.raises(AccessError) as error:
        await call(c, exams.change_session_state, state_request(first["session"], "ready"))
    assert error.value.code == "exam_variants_incomplete"
    with pytest.raises(AccessError):
        await call(c, exams.bind_paper, bind_request(c, first["session"]))
    # Explicit distinct second locked version, never inferred by question text or number.
    other = await create(c, title="合成B卷")
    other = await lock(c, other)
    b = bind_request(c, first["session"], variant_code="B").model_copy(
        update={"paper_version_id": UUID(other["paper_version_id"])}
    )
    second = await call(c, exams.bind_paper, b)
    with pytest.raises(AccessError):
        await call(
            c,
            exams.edit_session,
            SessionEdit(
                request_id=uuid4(),
                exam_session_id=s["id"],
                expected_revision=second["session"]["revision"],
                title=s["title"],
                starts_at=s["starts_at"],
                ends_at=s["ends_at"],
                paper_policy="single",
            ),
        )
    removed = await call(
        c,
        exams.unbind_paper,
        PaperUnbind(
            request_id=uuid4(),
            exam_session_id=s["id"],
            expected_revision=second["session"]["revision"],
            session_paper_id=second["binding"]["id"],
        ),
    )
    rebound = await call(
        c,
        exams.bind_paper,
        b.model_copy(
            update={"request_id": uuid4(), "expected_revision": removed["session"]["revision"]}
        ),
    )
    ready = await call(c, exams.change_session_state, state_request(rebound["session"], "ready"))
    with pytest.raises(AccessError):
        await call(
            c,
            exams.unbind_paper,
            PaperUnbind(
                request_id=uuid4(),
                exam_session_id=s["id"],
                expected_revision=ready["revision"],
                session_paper_id=first["binding"]["id"],
            ),
        )
    view = await call(c, exams.inspect_session, UUID(s["id"]))
    assert len(view["papers"]) == 3 and sum(p["status"] == "revoked" for p in view["papers"]) == 1
    assert view["default_variant"] == "A"


@pytest.mark.anyio
async def test_draft_and_retired_paper_rejected_history_not_rewritten(exam):
    c = exam
    s = await new_session(c)
    draft = await create(c, title="合成未锁卷")
    req = bind_request(c, s)
    with pytest.raises(AccessError):
        await call(
            c,
            exams.bind_paper,
            req.model_copy(update={"paper_version_id": UUID(draft["paper_version_id"])}),
        )
    accepted = await call(c, exams.bind_paper, req)
    async with c.factory() as db:
        await paper_service.retire_paper(db, c.principals["admin"], write(c.paper))
    view = await call(c, exams.inspect_session, UUID(s["id"]))
    assert view["papers"][0]["paper_status"] == "retired"
    assert (
        view["papers"][0]["id"] == accepted["binding"]["id"]
        and view["papers"][0]["status"] == "active"
    )
    with pytest.raises(AccessError):
        await call(c, exams.change_session_state, state_request(accepted["session"], "ready"))
    second = await new_session(c, offset=3)
    with pytest.raises(AccessError) as e:
        await call(c, exams.bind_paper, bind_request(c, second))
    assert e.value.code == "exam_paper_not_usable"


@pytest.mark.anyio
async def test_states_schedule_and_true_foreign_keys(exam):
    c = exam
    s = await new_session(c)
    request = SessionCreate(
        request_id=uuid4(),
        exam_event_id=c.event["id"],
        subject_id=c.subject_ids[0],
        expected_event_revision=(await read_event(c))["revision"],
        title="越界",
        starts_at="2027-02-01T01:00:00Z",
        ends_at="2027-02-01T02:00:00Z",
    )
    with pytest.raises(AccessError) as e:
        await call(c, exams.create_session, request)
    assert e.value.code == "exam_time_outside_term"
    with pytest.raises(AccessError):
        await call(c, exams.create_session, request.model_copy(update={"subject_id": uuid4()}))
    with pytest.raises(AccessError):
        await call(c, exams.change_session_state, state_request(s, "closed"))
    bound = await call(c, exams.bind_paper, bind_request(c, s))
    ready = await call(c, exams.change_session_state, state_request(bound["session"], "ready"))
    e = await read_event(c)
    active = await call(
        c,
        exams.change_event_state,
        EventState(
            request_id=uuid4(),
            exam_event_id=e["id"],
            expected_revision=e["revision"],
            lifecycle="active",
        ),
    )
    closed = await call(c, exams.change_session_state, state_request(ready, "closed"))
    end = await call(
        c,
        exams.change_event_state,
        EventState(
            request_id=uuid4(),
            exam_event_id=e["id"],
            expected_revision=active["revision"],
            lifecycle="closed",
        ),
    )
    assert closed["lifecycle"] == end["lifecycle"] == "closed"
    async with c.factory() as db:
        with pytest.raises(DBAPIError):
            await db.execute(
                update(ExamSession)
                .where(ExamSession.id == UUID(s["id"]))
                .values(exam_event_id=uuid4(), revision=closed["revision"] + 1)
            )
        await db.rollback()
        with pytest.raises(DBAPIError):
            await db.execute(
                insert(SecurityScope).values(
                    id=uuid4(), kind="exam_session", exam_session_id=uuid4()
                )
            )
        await db.rollback()
        with pytest.raises(DBAPIError):
            await db.execute(
                delete(SessionPaper).where(SessionPaper.id == UUID(bound["binding"]["id"]))
            )


@pytest.mark.anyio
async def test_scoped_management_paper_use_and_no_answer_read_authority(exam):
    c = exam
    s = await new_session(c)
    other = await new_session(c, offset=3)
    for user in ("a", "b"):
        with pytest.raises(AccessError):
            await call(c, exams.inspect_event, UUID(c.event["id"]), user)
    manager = await grant(c, SESSION_MANAGER_ROLE, s["id"])
    assert (await call(c, exams.inspect_session, UUID(s["id"]), "a"))["id"] == s["id"]
    with pytest.raises(AccessError):
        await call(c, exams.bind_paper, bind_request(c, s), "a")
    await grant(c, PAPER_USER_ROLE, c.paper["paper_version_id"], "paper_version")
    bound = await call(c, exams.bind_paper, bind_request(c, s), "a")
    with pytest.raises(AccessError):
        await call(c, paper_service.inspect_paper, UUID(c.paper["paper_version_id"]), "a")
    with pytest.raises(AccessError):
        await call(c, exams.bind_paper, bind_request(c, other), "a")
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(manager["grant_id"])
        )
    with pytest.raises(AccessError):
        await call(c, exams.change_session_state, state_request(bound["session"], "ready"), "a")
    # Removing explicit new grants makes legacy is_admin powerless.
    async with c.factory() as db, db.begin():
        await db.execute(
            update(SecurityGrant)
            .where(SecurityGrant.user_id == c.users["admin"].id)
            .values(revoked_at=func.clock_timestamp())
        )
    with pytest.raises(AccessError):
        await read_event(c)


@pytest.mark.anyio
async def test_event_scope_covers_only_its_real_children(exam):
    c = exam
    s = await new_session(c)
    await grant(c, EXAM_MANAGER_ROLE, c.event["id"], "exam_event")
    assert (await call(c, exams.inspect_session, UUID(s["id"]), "a"))["id"] == s["id"]
    e = await call(
        c,
        exams.create_event,
        EventCreate(request_id=uuid4(), academic_term_id=c.term_id, title="不同考试"),
    )
    other = await new_session(c, event_id=e["id"], offset=5)
    with pytest.raises(AccessError):
        await call(c, exams.inspect_session, UUID(other["id"]), "a")


@pytest.mark.anyio
async def test_idempotency_body_conflict_concurrent_same_code_and_stale_edit(exam):
    c = exam
    s = await new_session(c)
    req = bind_request(c, s)
    first = await call(c, exams.bind_paper, req)
    assert first == await call(c, exams.bind_paper, req)
    with pytest.raises(AccessError) as e:
        await call(c, exams.bind_paper, req.model_copy(update={"variant_code": "B"}))
    assert e.value.code == "exam_request_conflict"
    second = await new_session(c, offset=3)
    barrier = asyncio.Barrier(2)

    async def compete():
        await barrier.wait()
        try:
            return await call(c, exams.bind_paper, bind_request(c, second))
        except AccessError as e:
            return e

    results = await asyncio.gather(compete(), compete())
    assert sum(isinstance(r, dict) for r in results) == 1
    async with c.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SessionPaper)
                .where(SessionPaper.exam_session_id == UUID(second["id"]))
            )
            == 1
        )
    with pytest.raises(AccessError) as e:
        await call(c, exams.bind_paper, bind_request(c, s))
    assert e.value.code == "exam_revision_conflict"


@pytest.mark.anyio
async def test_different_sessions_concurrently_reuse_without_copy(exam):
    c = exam
    a = await new_session(c)
    b = await new_session(c, offset=3)
    barrier = asyncio.Barrier(2)

    async def run(s):
        await barrier.wait()
        return await call(c, exams.bind_paper, bind_request(c, s))

    results = await asyncio.gather(run(a), run(b))
    assert all(r["binding"]["paper_version_id"] == c.paper["paper_version_id"] for r in results)


@pytest.mark.anyio
@pytest.mark.parametrize("retire_first", [True, False])
async def test_binding_retirement_serializes_with_observed_database_lock(
    exam, monkeypatch, retire_first
):
    c = exam
    s = await new_session(c)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = exams.row if not retire_first else paper_service.validate_write
    if retire_first:

        async def held(db, req, **kw):
            r = await original(db, req, **kw)
            entered.set()
            await release.wait()
            return r

        monkeypatch.setattr(paper_service, "validate_write", held)

        async def retire():
            async with c.factory() as db:
                return await paper_service.retire_paper(db, c.principals["admin"], write(c.paper))

        first = asyncio.create_task(retire())
        await entered.wait()
        second = asyncio.create_task(call(c, exams.bind_paper, bind_request(c, s)))
        await wait_for_pg_lock(c.factory, "paper_versions")
    else:

        async def held(db, table, identifier, **kw):
            r = await original(db, table, identifier, **kw)
            if table is exams.P and kw.get("share"):
                entered.set()
                await release.wait()
            return r

        monkeypatch.setattr(exams, "row", held)
        first = asyncio.create_task(call(c, exams.bind_paper, bind_request(c, s)))
        await entered.wait()

        async def retire():
            async with c.factory() as db:
                return await paper_service.retire_paper(db, c.principals["admin"], write(c.paper))

        second = asyncio.create_task(retire())
        await wait_for_pg_lock(c.factory, "paper_versions")
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], dict)
    if retire_first:
        assert isinstance(results[1], AccessError)
    else:
        assert isinstance(results[1], dict)
    view = await call(c, exams.inspect_session, UUID(s["id"]))
    assert len(view["papers"]) == (0 if retire_first else 1)
    if view["papers"]:
        assert view["papers"][0]["paper_status"] == "retired"


@pytest.mark.anyio
async def test_audit_failure_rolls_back_and_lost_commit_ack_recovers(exam, monkeypatch):
    c = exam
    s = await new_session(c)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO exam_operations"):
            raise OperationalError("redacted", {}, Exception("synthetic"))

    async with c.factory() as db:
        event.listen(db.bind.sync_engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(AccessError):
                await exams.bind_paper(db, c.principals["admin"], bind_request(c, s))
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", fail)
    view = await call(c, exams.inspect_session, UUID(s["id"]))
    assert not view["papers"] and view["revision"] == s["revision"]
    request = bind_request(c, s)
    async with c.factory() as db:
        commit = db.commit

        async def lost():
            await commit()
            raise OperationalError("redacted", {}, Exception("lost ack"))

        monkeypatch.setattr(db, "commit", lost)
        with pytest.raises(AccessError) as e:
            await exams.bind_paper(db, c.principals["admin"], request)
        assert e.value.code == "exam_commit_outcome_unknown"
    replay = await call(c, exams.bind_paper, request)
    assert replay["session"]["revision"] == s["revision"] + 1
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(SessionPaper)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ExamOperation)
                .where(ExamOperation.request_id == request.request_id)
            )
            == 1
        )


@pytest.mark.anyio
async def test_revoke_waits_for_inflight_modification_then_rejects_new_requests(exam, monkeypatch):
    c = exam
    s = await new_session(c)
    g = await grant(c, SESSION_MANAGER_ROLE, s["id"])
    await grant(c, PAPER_USER_ROLE, c.paper["paper_version_id"], "paper_version")
    entered = asyncio.Event()
    release = asyncio.Event()
    original = exams.bump

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(exams, "bump", held)
    active = asyncio.create_task(call(c, exams.bind_paper, bind_request(c, s), "a"))
    await entered.wait()

    async def revoke():
        async with c.factory() as db:
            return await security.revoke_grant(
                db, c.principals["admin"], uuid4(), grant_id=UUID(g["grant_id"])
            )

    revoking = asyncio.create_task(revoke())
    await wait_for_pg_lock(c.factory, "security_grants")
    release.set()
    result, _ = await asyncio.gather(active, revoking)
    with pytest.raises(AccessError):
        await call(c, exams.change_session_state, state_request(result["session"], "ready"), "a")


@pytest.mark.anyio
async def test_incomplete_grants_cannot_union_into_session_write_authority(exam):
    c = exam
    s = await new_session(c)
    async with c.factory() as db, db.begin():
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(s["id"]))
        )
        for code in ["exam.session.manage", "exam.session.paper.bind"]:
            rid = uuid4()
            db.add(SecurityRoleVersion(id=rid, role_code="synthetic-" + rid.hex, revision=1))
            await db.flush()
            db.add(SecurityRolePermission(role_version_id=rid, permission_code=code))
            await db.flush()
            await db.execute(
                update(SecurityRoleVersion)
                .where(SecurityRoleVersion.id == rid)
                .values(sealed_at=func.clock_timestamp())
            )
            db.add(SecurityGrant(user_id=c.users["a"].id, role_version_id=rid, scope_id=scope))
    await grant(c, PAPER_USER_ROLE, c.paper["paper_version_id"], "paper_version")
    with pytest.raises(AccessError):
        await call(c, exams.bind_paper, bind_request(c, s), "a")
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(SessionPaper)) == 0


@pytest.mark.anyio
async def test_two_editors_same_revision_have_one_complete_winner(exam):
    c = exam
    s = await new_session(c)
    barrier = asyncio.Barrier(2)

    async def edit(title):
        await barrier.wait()
        try:
            return await call(
                c,
                exams.edit_session,
                SessionEdit(
                    request_id=uuid4(),
                    exam_session_id=s["id"],
                    expected_revision=s["revision"],
                    title=title,
                    starts_at=s["starts_at"],
                    ends_at=s["ends_at"],
                ),
            )
        except AccessError as e:
            return e

    results = await asyncio.gather(edit("甲"), edit("乙"))
    winners = [r for r in results if isinstance(r, dict)]
    assert len(winners) == 1
    data = await call(c, exams.inspect_session, UUID(s["id"]))
    assert data["title"] == winners[0]["title"] and data["revision"] == s["revision"] + 1


@pytest.mark.anyio
async def test_cli_signed_jwt_and_forged_role_never_authorize(exam, monkeypatch):
    import argparse
    import io
    import sys

    from huipi_cloud.modules.exams import __main__ as cli

    c = exam
    s = await new_session(c)
    monkeypatch.setattr(cli, "session_factory", c.factory)
    monkeypatch.setattr(cli, "TokenVerifier", lambda settings: c.verifier)
    args = argparse.Namespace(command="inspect-session", token_stdin=True, id=UUID(s["id"]))
    for name, success in [("admin", True), ("a", False)]:
        token = c.headers(name, role="exam_operator", is_admin=True)["Authorization"].split(" ", 1)[
            1
        ]
        monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
        if success:
            assert (await cli.run(args))["id"] == s["id"]
        else:
            with pytest.raises(AccessError) as e:
                await cli.run(args)
            assert e.value.code == "resource_not_found_or_forbidden"


@pytest.mark.anyio
async def test_database_failure_is_safe_and_no_success_audit(exam):
    c = exam
    s = await new_session(c)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if "security_grants" in statement:
            raise OperationalError("private server detail", {}, Exception("synthetic"))

    async with c.factory() as db:
        event.listen(db.bind.sync_engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(AccessError) as e:
                await exams.bind_paper(db, c.principals["admin"], bind_request(c, s))
            assert e.value.status_code == 503 and "private" not in e.value.code
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", fail)
    assert not (await call(c, exams.inspect_session, UUID(s["id"])))["papers"]


@pytest.mark.anyio
async def test_direct_sql_checks_real_parent_subject_digest_and_variant_unique(exam):
    c = exam
    s = await new_session(c)
    base = dict(
        id=uuid4(),
        exam_session_id=UUID(s["id"]),
        paper_version_id=UUID(c.paper["paper_version_id"]),
        variant_code="A",
        content_sha256=c.paper["content_sha256"],
        mapping_digest=c.paper["mapping_digest"],
        created_by_user_id=c.users["admin"].id,
    )
    async with c.factory() as db:
        for change in (
            {"paper_version_id": uuid4()},
            {"exam_session_id": uuid4()},
            {"content_sha256": "b" * 64},
            {"variant_code": "B"},
        ):
            with pytest.raises(DBAPIError):
                await db.execute(insert(SessionPaper).values(**dict(base, **change)))
            await db.rollback()
        await db.execute(insert(SessionPaper).values(**base))
        await db.commit()
        with pytest.raises(DBAPIError) as e:
            await db.execute(insert(SessionPaper).values(**dict(base, id=uuid4())))
        assert getattr(e.value.orig, "sqlstate", None) == "23505"
        await db.rollback()
        session = dict(
            id=uuid4(),
            exam_event_id=UUID(c.event["id"]),
            subject_id=uuid4(),
            title="错误学科",
            starts_at=datetime(2026, 11, 2, 1, tzinfo=UTC),
            ends_at=datetime(2026, 11, 2, 2, tzinfo=UTC),
            created_by_user_id=c.users["admin"].id,
        )
        with pytest.raises(DBAPIError):
            await db.execute(insert(ExamSession).values(**session))
        await db.rollback()
        with pytest.raises(DBAPIError):
            await db.execute(
                insert(ExamSession).values(
                    **dict(session, exam_event_id=uuid4(), subject_id=c.subject_ids[0])
                )
            )
        await db.rollback()
        from huipi_cloud.modules.exams.models import ExamEvent

        with pytest.raises(DBAPIError):
            await db.execute(
                insert(ExamEvent).values(
                    id=uuid4(),
                    academic_term_id=uuid4(),
                    title="缺失学期",
                    category="other",
                    organizer_user_id=c.users["admin"].id,
                    created_by_user_id=c.users["admin"].id,
                )
            )


@pytest.mark.anyio
async def test_migration_roundtrip_old_graph_paper_history_and_destructive_guard(
    papers, postgres_engine
):
    import hashlib
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from huipi_cloud.infrastructure.database.base import Base

    c = papers
    source = {}
    async with c.factory() as db:
        for table in Base.metadata.sorted_tables:
            if (
                table.name.startswith(("paper_", "exam_", "security_", "permission_"))
                or table.name in {"session_papers", "exam_participations"}
                or table.name.startswith("roster_")
            ):
                continue
            source[table.name] = [dict(r) for r in (await db.execute(select(table))).mappings()]
    name = "huipi_exam_roundtrip_" + uuid4().hex[:10] + "_test"
    url = postgres_engine.url.set(database=name)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    target = create_async_engine(url)
    env = dict(os.environ, DATABASE_URL=url.render_as_string(hide_password=False))

    async def migrate(*args, success=True):
        r = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", *args],
            env=env,
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
        )
        if success:
            assert r.returncode == 0, r.stderr[-1600:]
        return r

    def sha(rows):
        return hashlib.sha256(
            json.dumps(
                sorted(rows, key=lambda r: str(r.get("id"))), sort_keys=True, default=str
            ).encode()
        ).hexdigest()

    async def snapshot(names):
        async with target.connect() as db:
            return {
                name: sha(
                    [
                        dict(r)
                        for r in (await db.execute(text('SELECT * FROM "' + name + '"'))).mappings()
                    ]
                )
                for name in names
            }

    try:
        async with admin.connect() as db:
            await db.execute(text('CREATE DATABASE "' + name + '"'))
        await migrate("upgrade", "b12a7e90c210")
        async with target.begin() as db:
            for table in Base.metadata.sorted_tables:
                if table.name in source and source[table.name]:
                    await db.execute(insert(table), source[table.name])
        before = await snapshot(source)
        await migrate("upgrade", "head")
        await migrate("check")
        assert await snapshot(source) == before
        # Create a genuine locked B-1.2a graph on the scratch database, then verify
        # the B-1.2b downgrade/re-upgrade never changes its content or identities.
        factory = async_sessionmaker(target, expire_on_commit=False)
        async with factory() as db:
            await security.deployment_bootstrap(
                db, c.users["admin"].id, uuid4(), "synthetic scratch bootstrap"
            )
        async with factory() as db:
            await paper_service.enable_operator(
                db, c.users["admin"].id, c.users["admin"].id, uuid4(), "synthetic scratch paper"
            )
        from huipi_cloud.modules.papers.schemas import SnapshotCreate

        async with factory() as db:
            v = await paper_service.create_snapshot(
                db,
                c.principals["admin"],
                SnapshotCreate(request_id=uuid4(), source_assignment_id=c.aid, title="迁移历史卷"),
            )
        async with factory() as db:
            await paper_service.lock_paper(db, c.principals["admin"], write(v))
        names = list(source) + [
            "paper_families",
            "paper_versions",
            "paper_question_mappings",
            "paper_operations",
        ]
        baseline = await snapshot(names)
        await target.dispose()
        await migrate("downgrade", "b12a7e90c210")
        assert await snapshot(names) == baseline
        await target.dispose()
        await migrate("upgrade", "head")
        await migrate("check")
        assert await snapshot(names) == baseline
        async with target.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT enabled FROM permission_definitions WHERE code='exam.session.read'"
                    )
                )
                is True
            )
            constraints = set(await db.scalars(text("SELECT conname FROM pg_constraint")))
            assert {
                "fk_security_scope_event",
                "fk_security_scope_session",
                "ck_exam_session_time",
            } <= constraints
        # Current-slice audit facts alone are sufficient to refuse destructive rollback.
        async with target.begin() as db:
            await db.execute(
                insert(ExamOperation).values(
                    id=uuid4(),
                    actor_user_id=c.users["admin"].id,
                    request_id=uuid4(),
                    action="enable_operator",
                    payload_sha256="a" * 64,
                    result={},
                )
            )
        await target.dispose()
        r = await migrate("downgrade", "b12a7e90c210", success=False)
        assert r.returncode != 0 and "exam_downgrade_requires_archived_new_data" in r.stderr
        async with target.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version")) == "b12c9d20e411"
            )
    finally:
        await target.dispose()
        async with admin.connect() as db:
            await db.execute(text('DROP DATABASE IF EXISTS "' + name + '"'))
        await admin.dispose()


@pytest.mark.anyio
async def test_retired_after_ready_blocks_activation_and_keeps_original_binding(exam):
    c = exam
    s = await new_session(c)
    bound = await call(c, exams.bind_paper, bind_request(c, s))
    ready = await call(c, exams.change_session_state, state_request(bound["session"], "ready"))
    async with c.factory() as db:
        await paper_service.retire_paper(db, c.principals["admin"], write(c.paper))
    e = await read_event(c)
    with pytest.raises(AccessError) as error:
        await call(
            c,
            exams.change_event_state,
            EventState(
                request_id=uuid4(),
                exam_event_id=e["id"],
                expected_revision=e["revision"],
                lifecycle="active",
            ),
        )
    assert error.value.code == "exam_paper_not_usable"
    data = await call(c, exams.inspect_session, UUID(s["id"]))
    assert data["lifecycle"] == "ready" and data["revision"] == ready["revision"]
    assert data["papers"][0]["id"] == bound["binding"]["id"]
    assert data["papers"][0]["content_sha256"] == bound["binding"]["content_sha256"]
    assert (await read_event(c))["lifecycle"] == "draft"
