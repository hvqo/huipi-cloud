"""Actual PostgreSQL batch atomicity, source locks and bounded transaction lifetime."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from test_exam_roster_batch import seed
from test_exam_rosters import create, entries, freeze, ready, req, view
from test_exam_rosters import roster as roster
from test_exam_sessions import bind_request, call, new_session
from test_exam_sessions import exam as exam
from test_paper_versions import papers as papers
from test_paper_versions import wait_for_pg_lock, write
from test_teacher_authorization import auth_api as auth_api

from huipi_cloud.modules.exam_rosters import operations
from huipi_cloud.modules.exam_rosters import service as rosters
from huipi_cloud.modules.exam_rosters.models import (
    ExamParticipation,
    RosterEntrySnapshot,
    RosterOperation,
    RosterRevision,
)
from huipi_cloud.modules.exam_rosters.schemas import RosterCorrection, RosterCreate, RosterReplace
from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.organization.models import Student
from huipi_cloud.modules.papers import service as papers_service


def request(c, selections):
    return RosterCreate(
        request_id=uuid4(),
        exam_session_id=c.session["id"],
        business_date="2026-11-01",
        reason="合成批量验证",
        entries=selections,
    )


async def counts(c):
    async with c.factory() as db:
        return [
            await db.scalar(select(func.count()).select_from(table))
            for table in (
                RosterRevision,
                RosterEntrySnapshot,
                ExamParticipation,
                RosterOperation,
            )
        ]


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["create", "replace", "correct"])
async def test_late_batch_failure_rolls_back_complete_operation(roster, action):
    c = roster
    selections = await seed(c, 600, 8)
    if action == "create":
        fn, payload = rosters.create_roster, request(c, selections)
        previous = None
    else:
        previous = await create(c)
        if action == "correct":
            await ready(c)
            previous = await freeze(c, previous)
            fn = rosters.correct_roster
            payload = RosterCorrection(
                **req(previous).model_dump(), reason="合成更正失败", entries=selections
            )
        else:
            fn = rosters.replace_entries
            payload = RosterReplace(**req(previous).model_dump(), entries=selections)
    before = await counts(c)
    old_page = await view(c, previous) if previous else None
    executions = 0

    def fail(connection, cursor, statement, parameters, context, many):
        nonlocal executions
        if statement.startswith("INSERT INTO roster_entry_snapshots"):
            executions += 1
            if executions == 3:
                raise OperationalError("synthetic", {}, Exception("late batch failure"))

    async with c.factory() as db:
        event.listen(db.bind.sync_engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(AccessError) as error:
                await fn(db, c.principals["admin"], payload)
            assert error.value.code == "roster_operation_unavailable"
            assert not db.in_transaction()
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", fail)
    assert executions == 3
    assert await counts(c) == before
    if previous:
        assert await view(c, previous) == old_page
    # Failed transactions consume neither the request ID nor partial stable identities.
    result = await call(c, fn, payload)
    assert (await view(c, result))["total"] == 600


@pytest.mark.anyio
async def test_same_session_serializes_and_different_sessions_share_students(roster, monkeypatch):
    c = roster
    selections = await seed(c, 400, 8)
    barrier = asyncio.Barrier(2)

    async def write_one(payload):
        await barrier.wait()
        try:
            return await call(c, rosters.create_roster, payload)
        except AccessError as error:
            return error

    a, b = await asyncio.gather(
        write_one(request(c, selections)), write_one(request(c, selections))
    )
    assert sum(isinstance(v, dict) for v in (a, b)) == 1
    assert next(v for v in (a, b) if isinstance(v, AccessError)).status_code == 409
    first = a if isinstance(a, dict) else b
    other = await new_session(c, offset=3)
    bound = await call(c, exams.bind_paper, bind_request(c, other))
    # Existing session replace and another session create both pause AFTER parent locks.
    original = rosters.materialize
    barrier = asyncio.Barrier(2)

    async def together(*args):
        await barrier.wait()
        return await original(*args)

    monkeypatch.setattr(rosters, "materialize", together)
    other_request = request(
        c, [dict(e, session_paper_id=bound["binding"]["id"]) for e in selections]
    ).model_copy(update={"exam_session_id": UUID(other["id"])})
    results = await asyncio.wait_for(
        asyncio.gather(
            call(
                c,
                rosters.replace_entries,
                RosterReplace(**req(first).model_dump(), entries=selections),
            ),
            call(c, rosters.create_roster, other_request),
        ),
        timeout=20,
    )
    assert [(await view(c, r))["total"] for r in results] == [400, 400]
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ExamParticipation)) == 800


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["student", "paper"])
@pytest.mark.parametrize("change_first", [True, False])
async def test_bulk_source_state_changes_serialize(roster, monkeypatch, source, change_first):
    c = roster
    payload = request(c, entries(c, 3))
    entered, release = asyncio.Event(), asyncio.Event()

    async def change():
        async with c.factory() as db:
            if source == "student":
                await db.execute(
                    update(Student).where(Student.id == c.students[0].id).values(is_active=False)
                )
                if change_first:
                    entered.set()
                    await release.wait()
                await db.commit()
            else:
                if change_first:
                    original_commit = db.commit

                    async def held_commit():
                        entered.set()
                        await release.wait()
                        await original_commit()

                    db.commit = held_commit
                await papers_service.retire_paper(db, c.principals["admin"], write(c.paper))

    if not change_first:
        original = rosters.insert_batches

        async def held(*args):
            if args[1] is rosters.E:
                entered.set()
                await release.wait()
            return await original(*args)

        monkeypatch.setattr(rosters, "insert_batches", held)
        first = asyncio.create_task(call(c, rosters.create_roster, payload))
        await asyncio.wait_for(entered.wait(), 10)
        second = asyncio.create_task(change())
    else:
        first = asyncio.create_task(change())
        await asyncio.wait_for(entered.wait(), 10)
        second = asyncio.create_task(call(c, rosters.create_roster, payload))
    try:
        await wait_for_pg_lock(c.factory, "students" if source == "student" else "paper_versions")
    finally:
        release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert not isinstance(results[0], BaseException), results
    if change_first:
        assert isinstance(results[1], AccessError)
        assert (await counts(c))[:3] == [0, 0, 0]
    else:
        assert results[1] is None
        assert (await view(c, results[0]))["total"] == 3
        await ready_if_usable(c, source)


async def ready_if_usable(c, source):
    if source == "student":
        await ready(c)
        async with c.factory() as db:
            rid = await db.scalar(select(RosterRevision.id))
        value = (await view(c, {"id": str(rid)}))["roster"]
        with pytest.raises(AccessError):
            await freeze(c, value)


@pytest.mark.anyio
async def test_total_budget_rolls_back_batches_and_releases_session(roster, monkeypatch):
    c = roster
    selections = await seed(c, 400, 8)
    payload = request(c, selections)
    before = await counts(c)
    original = rosters.insert_batches
    entered = asyncio.Event()

    async def stalled(*args):
        await original(*args)
        if args[1] is rosters.E:
            entered.set()
            await asyncio.Event().wait()  # deterministic stall, not a database sleep

    monkeypatch.setattr(rosters, "insert_batches", stalled)
    monkeypatch.setattr(operations.settings, "roster_operation_timeout_seconds", 1)
    async with c.factory() as db:
        task = asyncio.create_task(rosters.create_roster(db, c.principals["admin"], payload))
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(AccessError) as error:
            await asyncio.wait_for(task, 5)
        assert error.value.code == "roster_operation_timeout"
        assert not db.in_transaction()
        assert await db.scalar(text("SELECT 1")) == 1
    assert await counts(c) == before
    monkeypatch.setattr(rosters, "insert_batches", original)
    assert (await view(c, await call(c, rosters.create_roster, payload)))["total"] == 400


@pytest.mark.anyio
async def test_commit_timeout_keeps_unknown_outcome_and_idempotent_recovery(roster):
    c = roster
    payload = request(c, entries(c, 3))
    async with c.factory() as db:
        original = db.commit

        # Transport timeout after a confirmed server COMMIT must not become known rollback.
        async def lost_timeout():
            await original()
            raise TimeoutError("synthetic lost commit confirmation")

        db.commit = lost_timeout
        with pytest.raises(AccessError) as error:
            await rosters.create_roster(db, c.principals["admin"], payload)
        assert error.value.code == "roster_commit_outcome_unknown"
    result = await call(c, rosters.create_roster, payload)
    assert (await view(c, result))["total"] == 3
    async with c.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RosterOperation)
                .where(RosterOperation.request_id == payload.request_id)
            )
            == 1
        )


@pytest.mark.anyio
async def test_bulk_constraints_remain_active_after_freeze(roster):
    c = roster
    selections = await seed(c, 400, 8)
    r = await call(c, rosters.create_roster, request(c, selections))
    await ready(c)
    frozen = await freeze(c, r)
    async with c.factory() as db:
        with pytest.raises(DBAPIError):
            await db.execute(
                update(RosterEntrySnapshot)
                .where(RosterEntrySnapshot.roster_revision_id == UUID(frozen["id"]))
                .values(exam_number="FORGED")
            )
        await db.rollback()
    assert (await view(c, frozen))["total"] == 400


@pytest.mark.anyio
async def test_lock_budget_rejects_waiter_without_partial_roster(roster, monkeypatch):
    c = roster
    monkeypatch.setattr(operations.settings, "roster_lock_timeout_seconds", 0.5)
    async with c.factory() as blocker:
        await blocker.execute(
            select(exams.S).where(exams.S.c.id == UUID(c.session["id"])).with_for_update()
        )
        task = asyncio.create_task(call(c, rosters.create_roster, request(c, entries(c))))
        try:
            await wait_for_pg_lock(c.factory, "exam_sessions")
            with pytest.raises(AccessError) as error:
                await asyncio.wait_for(task, 5)
            assert error.value.code == "roster_operation_unavailable"
        finally:
            await blocker.rollback()
    assert (await counts(c))[:3] == [0, 0, 0]


@pytest.mark.anyio
async def test_external_cancellation_rolls_back_completed_batch(roster, monkeypatch):
    c = roster
    before = await counts(c)
    entered = asyncio.Event()
    original = rosters.insert_batches

    async def pause(*args):
        await original(*args)
        if args[1] is rosters.E:
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(rosters, "insert_batches", pause)
    async with c.factory() as db:
        task = asyncio.create_task(
            rosters.create_roster(db, c.principals["admin"], request(c, entries(c)))
        )
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not db.in_transaction()
    assert await counts(c) == before


@pytest.mark.anyio
async def test_source_rows_are_fresh_even_with_stale_orm_identity_map(roster):
    c = roster
    async with c.factory() as db:
        stale = await db.get(Student, c.students[0].id)
        await db.commit()  # expire_on_commit=False leaves this ORM object in the identity map
        async with c.factory() as writer, writer.begin():
            await writer.execute(
                update(Student).where(Student.id == stale.id).values(name="合成新姓名")
            )
        assert stale.name != "合成新姓名"
        result = await rosters.create_roster(db, c.principals["admin"], request(c, entries(c)))
    rows = (await view(c, result))["entries"]
    assert next(r for r in rows if r["student_id"] == str(stale.id))["student_name"] == "合成新姓名"
