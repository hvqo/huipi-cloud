"""Real PostgreSQL roster snapshots, current grants and deterministic concurrency."""

import asyncio
from datetime import date, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from test_exam_sessions import bind_request, call, new_session, state_request
from test_exam_sessions import exam as exam
from test_paper_versions import papers as papers
from test_paper_versions import wait_for_pg_lock, write
from test_teacher_authorization import auth_api as auth_api

from huipi_cloud.modules.exam_rosters import service as rosters
from huipi_cloud.modules.exam_rosters.models import (
    ExamParticipation,
    RosterEntrySnapshot,
    RosterOperation,
    RosterRevision,
)
from huipi_cloud.modules.exam_rosters.permissions import (
    ROSTER_FREEZER_ROLE,
    ROSTER_MANAGER_ROLE,
    ROSTER_READER_ROLE,
)
from huipi_cloud.modules.exam_rosters.schemas import (
    RosterCorrection,
    RosterCreate,
    RosterReplace,
    RosterWrite,
)
from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.organization.models import (
    AdministrativeClass,
    ClassEnrollment,
    GradeCohort,
    Student,
)
from huipi_cloud.modules.papers import service as paper_service
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityGrant, SecurityScope


@pytest.fixture
async def roster(exam):
    c = exam
    async with c.factory() as db:
        await rosters.enable_operator(
            db, c.users["admin"].id, c.users["admin"].id, uuid4(), "synthetic roster approval"
        )
    c.session = await new_session(c)
    bound = await call(c, exams.bind_paper, bind_request(c, c.session))
    c.binding = bound["binding"]
    c.session = bound["session"]
    async with c.factory() as db, db.begin():
        cohort = GradeCohort(code=uuid4().hex, display_name="合成届别", intake_year=2025)
        db.add(cohort)
        await db.flush()
        c.classes = [
            AdministrativeClass(
                grade_cohort_id=cohort.id, code=uuid4().hex, display_name=f"合成{i}班"
            )
            for i in (1, 2)
        ]
        c.students = [Student(school_student_code=uuid4().hex, name="合成同名") for _ in range(3)]
        db.add_all(c.classes + c.students)
        await db.flush()
        c.enrollments = [
            ClassEnrollment(
                student_id=s.id,
                administrative_class_id=c.classes[i % 2].id,
                valid_from=date(2026, 9, 1),
            )
            for i, s in enumerate(c.students)
        ]
        db.add_all(c.enrollments)
    yield c


def entries(c, count=2, unknown=False):
    return [
        dict(
            student_id=s.id,
            exam_number=f"N{i + 1:03}",
            session_paper_id=None if unknown else c.binding["id"],
        )
        for i, s in enumerate(c.students[:count])
    ]


async def create(c, values=None):
    return await call(
        c,
        rosters.create_roster,
        RosterCreate(
            request_id=uuid4(),
            exam_session_id=c.session["id"],
            business_date="2026-11-01",
            reason="合成初始名单",
            entries=entries(c) if values is None else values,
        ),
    )


def req(value):
    return RosterWrite(
        request_id=uuid4(), roster_revision_id=value["id"], expected_revision=value["revision"]
    )


async def view(c, value, name="admin", **kwargs):
    async with c.factory() as db:
        return await rosters.inspect_roster(db, c.principals[name], UUID(value["id"]), **kwargs)


async def ready(c):
    c.session = await call(c, exams.change_session_state, state_request(c.session, "ready"))


async def freeze(c, value):
    return await call(c, rosters.freeze_roster, req(value))


async def grant(c, role=ROSTER_MANAGER_ROLE, session_id=None, name="a"):
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(
                SecurityScope.exam_session_id == UUID(session_id or c.session["id"])
            )
        )
    async with c.factory() as db:
        return await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users[name].id,
            role_version_id=role,
            scope_id=scope,
        )


@pytest.mark.anyio
async def test_two_classes_same_names_shared_paper_and_paginated_read(roster):
    c = roster
    r = await create(c)
    data = await view(c, r, limit=1)
    assert data["total"] == 2 and data["truncated"] and data["attendance_status"] == "not_recorded"
    data = await view(c, r)
    assert len({x["student_id"] for x in data["entries"]}) == 2
    assert len({x["administrative_class_id"] for x in data["entries"]}) == 2
    assert {x["student_name"] for x in data["entries"]} == {"合成同名"}
    assert {x["session_paper_id"] for x in data["entries"]} == {c.binding["id"]}
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ExamParticipation)) == 2
    await ready(c)
    f = await freeze(c, r)
    assert f["is_current"] and f["status"] == "frozen"


@pytest.mark.anyio
async def test_transfer_keeps_frozen_snapshot_and_correction_history(roster):
    c = roster
    r = await create(c)
    await ready(c)
    f = await freeze(c, r)
    before = await view(c, f)
    async with c.factory() as db, db.begin():
        await db.execute(
            update(ClassEnrollment)
            .where(ClassEnrollment.id == c.enrollments[0].id)
            .values(valid_to=date(2026, 12, 1))
        )
        db.add(
            ClassEnrollment(
                student_id=c.students[0].id,
                administrative_class_id=c.classes[1].id,
                valid_from=date(2026, 12, 1),
            )
        )
    correction = RosterCorrection(
        **req(f).model_dump(),
        reason="合成考号更正与移除增补",
        entries=[{**entries(c)[0], "exam_number": "NEW1"}, entries(c, 3)[2]],
    )
    second = await call(c, rosters.correct_roster, correction)
    old = await view(c, f)
    assert old == before
    new = await view(c, second)
    original = next(x for x in before["entries"] if x["student_id"] == str(c.students[0].id))
    changed = next(x for x in new["entries"] if x["student_id"] == str(c.students[0].id))
    assert (
        original["administrative_class_id"]
        == changed["administrative_class_id"]
        == str(c.classes[0].id)
    )
    assert original["participation_id"] == changed["participation_id"]
    f2 = await freeze(c, second)
    assert f2["previous_revision_id"] == f["id"] and f2["version_no"] == 2
    old = await view(c, f)
    assert old["entries"] == before["entries"] and not old["roster"]["is_current"]
    assert old["roster"]["revision"] == f["revision"]
    async with c.factory() as db:
        h = await rosters.history(db, c.principals["admin"], UUID(c.session["id"]))
    assert [v["version_no"] for v in h["revisions"]] == [1, 2]


@pytest.mark.anyio
async def test_unknown_variant_freeze_not_attendance_and_exam_number_required(roster):
    c = roster
    r = await create(c, [{**entries(c, unknown=True)[0], "exam_number": None}])
    await ready(c)
    with pytest.raises(AccessError):
        await freeze(c, r)
    assert (await view(c, r))["roster"]["status"] == "draft"
    r = await call(
        c,
        rosters.replace_entries,
        RosterReplace(**req(r).model_dump(), entries=entries(c, unknown=True)),
    )
    f = await freeze(c, r)
    data = await view(c, f)
    assert data["unknown_variant_count"] == 2 and data["attendance_status"] == "not_recorded"
    assert all(e["session_paper_id"] is None for e in data["entries"])


@pytest.mark.anyio
async def test_cross_session_revoked_and_retired_bindings_rejected(roster):
    c = roster
    other = await new_session(c, offset=3)
    otherbinding = await call(c, exams.bind_paper, bind_request(c, other))
    for bid in (otherbinding["binding"]["id"], str(uuid4())):
        with pytest.raises(AccessError):
            await create(c, [{**entries(c)[0], "session_paper_id": bid}])
    from huipi_cloud.modules.exams.schemas import PaperUnbind

    await call(
        c,
        exams.unbind_paper,
        PaperUnbind(
            request_id=uuid4(),
            exam_session_id=c.session["id"],
            expected_revision=c.session["revision"],
            session_paper_id=c.binding["id"],
        ),
    )
    with pytest.raises(AccessError):
        await create(c)
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(RosterRevision)) == 0


@pytest.mark.anyio
async def test_retire_and_cancel_refuse_freeze_keep_history(roster):
    c = roster
    r = await create(c)
    await ready(c)
    async with c.factory() as db:
        await paper_service.retire_paper(db, c.principals["admin"], write(c.paper))
    with pytest.raises(AccessError):
        await freeze(c, r)
    assert (await view(c, r))["roster"]["status"] == "draft"
    await call(c, exams.change_session_state, state_request(c.session, "cancelled"))
    with pytest.raises(AccessError):
        await call(c, rosters.replace_entries, RosterReplace(**req(r).model_dump(), entries=[]))
    assert (await view(c, r))["total"] == 2


@pytest.mark.anyio
async def test_direct_sql_frozen_entries_identity_numbers_and_current_guards(roster):
    c = roster
    r = await create(c)
    data = await view(c, r)
    row = data["entries"][0]
    async with c.factory() as db:
        raw = dict(
            (
                await db.execute(
                    select(RosterEntrySnapshot.__table__).where(
                        RosterEntrySnapshot.id == UUID(row["id"])
                    )
                )
            )
            .mappings()
            .one()
        )
        await db.rollback()
        for change in (
            {"exam_number": data["entries"][1]["exam_number"]},
            {"student_name": "伪造姓名"},
            {"student_id": c.students[1].id},
            {"exam_session_id": uuid4()},
        ):
            with pytest.raises(DBAPIError):
                await db.execute(
                    insert(RosterEntrySnapshot).values(**dict(raw, id=uuid4(), **change))
                )
            await db.rollback()
    await ready(c)
    f = await freeze(c, r)
    async with c.factory() as db:
        for stmt in (
            update(RosterEntrySnapshot)
            .where(RosterEntrySnapshot.id == UUID(row["id"]))
            .values(exam_number="BAD"),
            delete(RosterEntrySnapshot).where(RosterEntrySnapshot.id == UUID(row["id"])),
            delete(RosterRevision).where(RosterRevision.id == UUID(r["id"])),
            update(ExamParticipation)
            .where(ExamParticipation.id == UUID(row["participation_id"]))
            .values(student_id=c.students[2].id),
        ):
            with pytest.raises(DBAPIError):
                await db.execute(stmt)
            await db.rollback()
        with pytest.raises(DBAPIError):
            await db.execute(
                update(RosterRevision)
                .where(RosterRevision.id == UUID(f["id"]))
                .values(is_current=False)
            )
            await db.commit()
        await db.rollback()
    assert (await view(c, r))["roster"]["is_current"]


@pytest.mark.anyio
async def test_scopes_roles_grant_revoke_and_replay(roster):
    c = roster
    r = await create(c)
    with pytest.raises(AccessError):
        await view(c, r, "a")
    g = await grant(c, ROSTER_READER_ROLE)
    assert (await view(c, r, "a"))["total"] == 2
    with pytest.raises(AccessError):
        await call(
            c, rosters.replace_entries, RosterReplace(**req(r).model_dump(), entries=[]), "a"
        )
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(g["grant_id"])
        )
    await grant(c)
    request = RosterReplace(**req(r).model_dump(), entries=entries(c, 1))
    result = await call(c, rosters.replace_entries, request, "a")
    assert await call(c, rosters.replace_entries, request, "a") == result
    with pytest.raises(AccessError) as e:
        await call(c, rosters.replace_entries, request.model_copy(update={"entries": []}), "a")
    assert e.value.code == "roster_request_conflict"
    async with c.factory() as db, db.begin():
        await db.execute(
            update(SecurityGrant)
            .where(SecurityGrant.user_id == c.users["a"].id, SecurityGrant.revoked_at.is_(None))
            .values(revoked_at=func.clock_timestamp())
        )
    with pytest.raises(AccessError):
        await call(c, rosters.replace_entries, request, "a")
    other = await new_session(c, offset=3)
    await grant(c, session_id=other["id"])
    with pytest.raises(AccessError):
        await view(c, r, "a")
    async with c.factory() as db, db.begin():
        await db.execute(
            update(SecurityGrant)
            .where(SecurityGrant.user_id == c.users["admin"].id, SecurityGrant.revoked_at.is_(None))
            .values(revoked_at=func.clock_timestamp())
        )
    with pytest.raises(AccessError):
        await view(c, r)


@pytest.mark.anyio
@pytest.mark.parametrize("freeze_first", [True, False])
async def test_edit_freeze_race_observed_row_lock(roster, monkeypatch, freeze_first):
    c = roster
    r = await create(c)
    await ready(c)
    first_fn = rosters.freeze_roster if freeze_first else rosters.replace_entries
    second_fn = rosters.replace_entries if freeze_first else rosters.freeze_roster
    replace = RosterReplace(**req(r).model_dump(), entries=entries(c, 1))
    first_req = req(r) if freeze_first else replace
    second_req = replace if freeze_first else req(r)
    reached, release = asyncio.Event(), asyncio.Event()
    original = rosters.locked_revision

    async def held(*args, **kw):
        result = await original(*args, **kw)
        if asyncio.current_task().get_name() == "first-roster-writer":
            reached.set()
            await release.wait()
        return result

    monkeypatch.setattr(rosters, "locked_revision", held)
    a = asyncio.create_task(call(c, first_fn, first_req), name="first-roster-writer")
    await reached.wait()
    b = asyncio.create_task(call(c, second_fn, second_req))
    try:
        await wait_for_pg_lock(c.factory, "exam_sessions")
    finally:
        release.set()
    results = await asyncio.gather(a, b, return_exceptions=True)
    assert isinstance(results[0], dict) and isinstance(results[1], AccessError)
    data = await view(c, r)
    assert data["total"] == (2 if freeze_first else 1)
    assert data["roster"]["status"] == ("frozen" if freeze_first else "draft")


@pytest.mark.anyio
async def test_audit_rollback_and_unknown_commit_recovery(roster, monkeypatch):
    c = roster
    r = await create(c)
    request = RosterReplace(**req(r).model_dump(), entries=entries(c, 1))

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO roster_operations"):
            raise OperationalError("redacted", {}, Exception("synthetic"))

    async with c.factory() as db:
        event.listen(db.bind.sync_engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(AccessError):
                await rosters.replace_entries(db, c.principals["admin"], request)
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", fail)
    assert (await view(c, r))["total"] == 2
    async with c.factory() as db:
        original = db.commit

        async def lost():
            await original()
            raise OperationalError("redacted", {}, Exception("synthetic lost ACK"))

        monkeypatch.setattr(db, "commit", lost)
        with pytest.raises(AccessError) as e:
            await rosters.replace_entries(db, c.principals["admin"], request)
        assert e.value.code == "roster_commit_outcome_unknown"
    result = await call(c, rosters.replace_entries, request)
    assert result["revision"] == r["revision"] + 1 and (await view(c, r))["total"] == 1
    async with c.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RosterOperation)
                .where(RosterOperation.request_id == request.request_id)
            )
            == 1
        )


@pytest.mark.anyio
async def test_freezer_cannot_edit_or_correct_and_schedule_is_pinned(roster):
    c = roster
    r = await create(c)
    await grant(c, ROSTER_FREEZER_ROLE)
    await ready(c)
    f = await call(c, rosters.freeze_roster, req(r), "a")
    with pytest.raises(AccessError):
        await call(
            c,
            rosters.correct_roster,
            RosterCorrection(**req(f).model_dump(), reason="不允许", entries=entries(c)),
            "a",
        )
    assert (await view(c, f, "a"))["roster"]["status"] == "frozen"


@pytest.mark.anyio
async def test_ab_assigned_and_unknown_mixed_no_guess(roster):
    from test_paper_versions import create as new_paper
    from test_paper_versions import lock

    from huipi_cloud.modules.exams.schemas import SessionEdit

    c = roster
    s = c.session
    s = await call(
        c,
        exams.edit_session,
        SessionEdit(
            request_id=uuid4(),
            exam_session_id=s["id"],
            expected_revision=s["revision"],
            title=s["title"],
            starts_at=s["starts_at"],
            ends_at=s["ends_at"],
            paper_policy="ab",
        ),
    )
    c.session = s
    p = await lock(c, await new_paper(c, title="合成B卷"))
    b = await call(
        c,
        exams.bind_paper,
        bind_request(c, s, variant_code="B").model_copy(
            update={"paper_version_id": UUID(p["paper_version_id"])}
        ),
    )
    c.session = b["session"]
    values = entries(c, 3)
    values[1]["session_paper_id"] = b["binding"]["id"]
    values[2]["session_paper_id"] = None
    r = await create(c, values)
    await ready(c)
    f = await freeze(c, r)
    data = await view(c, f)
    assert {e["session_paper_id"] for e in data["entries"]} == {
        c.binding["id"],
        b["binding"]["id"],
        None,
    }
    assert data["unknown_variant_count"] == 1


@pytest.mark.anyio
async def test_correction_current_and_participation_unique_under_race(roster):
    c = roster
    r = await create(c)
    await ready(c)
    f = await freeze(c, r)
    barrier = asyncio.Barrier(2)

    async def compete():
        await barrier.wait()
        try:
            return await call(
                c,
                rosters.correct_roster,
                RosterCorrection(**req(f).model_dump(), reason="并发合成更正", entries=entries(c)),
            )
        except AccessError as e:
            return e

    result = await asyncio.gather(compete(), compete())
    assert sum(isinstance(x, dict) for x in result) == 1
    child = next(x for x in result if isinstance(x, dict))
    assert (await view(c, f))["roster"]["is_current"]
    await freeze(c, child)
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ExamParticipation)) == 2
        assert (
            await db.scalar(
                select(func.count()).select_from(RosterRevision).where(RosterRevision.is_current)
            )
            == 1
        )


@pytest.mark.anyio
async def test_missing_enrollment_business_date_and_source_change_fail_closed(roster):
    c = roster
    with pytest.raises(AccessError):
        await call(
            c,
            rosters.create_roster,
            RosterCreate(
                request_id=uuid4(),
                exam_session_id=c.session["id"],
                business_date="2026-10-31",
                reason="错误日期",
                entries=entries(c),
            ),
        )
    with pytest.raises(AccessError):
        await create(c, [dict(student_id=uuid4(), exam_number="X")])
    r = await create(c)
    async with c.factory() as db, db.begin():
        await db.execute(
            update(ClassEnrollment)
            .where(ClassEnrollment.id == c.enrollments[0].id)
            .values(valid_to=date(2026, 10, 31))
        )
    await ready(c)
    with pytest.raises(AccessError):
        await freeze(c, r)
    assert (await view(c, r))["roster"]["status"] == "draft"
    async with c.factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RosterOperation)
                .where(
                    RosterOperation.action == "freeze",
                    RosterOperation.actor_user_id == c.users["admin"].id,
                )
            )
            == 0
        )


@pytest.mark.anyio
async def test_revoke_waits_for_inflight_roster_write_and_blocks_replay(roster, monkeypatch):
    c = roster
    r = await create(c)
    g = await grant(c)
    request = RosterReplace(**req(r).model_dump(), entries=entries(c, 1))
    reached, release = asyncio.Event(), asyncio.Event()
    original = rosters.materialize

    async def held(*args, **kw):
        await original(*args, **kw)
        reached.set()
        await release.wait()

    monkeypatch.setattr(rosters, "materialize", held)
    task = asyncio.create_task(call(c, rosters.replace_entries, request, "a"))
    await reached.wait()

    async def revoke():
        async with c.factory() as db:
            return await security.revoke_grant(
                db, c.principals["admin"], uuid4(), grant_id=UUID(g["grant_id"])
            )

    revoked = asyncio.create_task(revoke())
    try:
        await wait_for_pg_lock(c.factory, "security_grants")
    finally:
        release.set()
    await asyncio.gather(task, revoked)
    assert (await view(c, r))["total"] == 1
    with pytest.raises(AccessError):
        await call(c, rosters.replace_entries, request, "a")


@pytest.mark.anyio
async def test_cli_real_jwt_role_claims_and_db_failure(roster, monkeypatch, tmp_path):
    import argparse
    import io

    from huipi_cloud.modules.exam_rosters import __main__ as cli

    c = roster
    monkeypatch.setattr(cli, "session_factory", c.factory)
    monkeypatch.setattr(cli, "TokenVerifier", lambda settings: c.verifier)
    r = await create(c)
    args = argparse.Namespace(
        command="inspect", token_stdin=True, id=UUID(r["id"]), offset=0, limit=100
    )
    token = c.headers("a", role="admin", is_admin=True)["Authorization"].split()[1]
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(token + "\n"))
    with pytest.raises(AccessError):
        await cli.run(args)
    await grant(c, ROSTER_READER_ROLE)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(token + "\n"))
    assert (await cli.run(args))["total"] == 2
    async with c.factory() as db:

        async def fail(*a, **kw):
            raise OperationalError("redacted", {}, Exception("synthetic db down"))

        monkeypatch.setattr(db, "execute", fail)
        with pytest.raises(AccessError) as e:
            await rosters.create_roster(
                db,
                c.principals["admin"],
                RosterCreate(
                    request_id=uuid4(),
                    exam_session_id=c.session["id"],
                    business_date="2026-11-01",
                    reason="测试故障",
                    entries=[],
                ),
            )
        assert e.value.code == "roster_operation_unavailable"


@pytest.mark.anyio
async def test_migration_roundtrip_old_exam_paper_graph_and_data_guard(exam, postgres_engine):
    import hashlib
    import json

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from test_organization_migration import alembic

    from huipi_cloud.infrastructure.database.base import Base
    from huipi_cloud.modules.exams.schemas import EventCreate
    from huipi_cloud.modules.papers.schemas import SnapshotCreate

    c = exam
    name = "huipi_roster_roundtrip_" + uuid4().hex[:10] + "_test"
    url = postgres_engine.url.set(database=name)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    target = create_async_engine(url)
    source = {}
    async with c.factory() as db:
        for table in Base.metadata.sorted_tables:
            if (
                table.name.startswith(("security_", "permission_", "paper_", "exam_", "roster_"))
                or table.name == "session_papers"
            ):
                continue
            source[table.name] = [dict(r) for r in (await db.execute(select(table))).mappings()]

    async def snapshot():
        async with target.connect() as db:
            return {
                name: hashlib.sha256(
                    json.dumps(
                        [
                            dict(r)
                            for r in (
                                await db.execute(text('SELECT * FROM "' + name + '" ORDER BY 1'))
                            ).mappings()
                        ],
                        sort_keys=True,
                        default=str,
                    ).encode()
                ).hexdigest()
                for name in names
            }

    try:
        async with admin.connect() as db:
            await db.execute(text('CREATE DATABASE "' + name + '"'))
        await alembic(url, "upgrade", "b12bf031a920")
        async with target.begin() as db:
            for table in Base.metadata.sorted_tables:
                if source.get(table.name):
                    await db.execute(insert(table), source[table.name])
        factory = async_sessionmaker(target, expire_on_commit=False)
        async with factory() as db:
            await security.deployment_bootstrap(
                db, c.users["admin"].id, uuid4(), "synthetic migration"
            )
        async with factory() as db:
            await paper_service.enable_operator(
                db, c.users["admin"].id, c.users["admin"].id, uuid4(), "synthetic paper"
            )
        async with factory() as db:
            p = await paper_service.create_snapshot(
                db,
                c.principals["admin"],
                SnapshotCreate(
                    request_id=uuid4(), source_assignment_id=c.aid, title="合成迁移历史卷"
                ),
            )
        async with factory() as db:
            p = await paper_service.lock_paper(db, c.principals["admin"], write(p))
        async with factory() as db:
            await exams.enable_operator(
                db, c.users["admin"].id, c.users["admin"].id, uuid4(), "synthetic exam"
            )
        async with factory() as db:
            e = await exams.create_event(
                db,
                c.principals["admin"],
                EventCreate(request_id=uuid4(), academic_term_id=c.term_id, title="合成历史考试"),
            )
        from huipi_cloud.modules.exams.schemas import PaperBind, SessionCreate

        async with factory() as db:
            s = await exams.create_session(
                db,
                c.principals["admin"],
                SessionCreate(
                    request_id=uuid4(),
                    exam_event_id=e["id"],
                    expected_event_revision=e["revision"],
                    subject_id=c.subject_ids[0],
                    title="合成场次",
                    starts_at="2026-11-01T09:00:00+08:00",
                    ends_at="2026-11-01T11:00:00+08:00",
                ),
            )
        async with factory() as db:
            await exams.bind_paper(
                db,
                c.principals["admin"],
                PaperBind(
                    request_id=uuid4(),
                    exam_session_id=s["id"],
                    expected_revision=s["revision"],
                    paper_version_id=p["paper_version_id"],
                ),
            )
        names = list(source) + [
            "paper_families",
            "paper_versions",
            "paper_question_mappings",
            "paper_operations",
            "exam_events",
            "exam_sessions",
            "session_papers",
            "exam_operations",
            "security_scopes",
            "security_grants",
            "security_audit_events",
        ]
        before = await snapshot()
        await target.dispose()
        await alembic(url, "upgrade", "head")
        await alembic(url, "check")
        assert await snapshot() == before
        await target.dispose()
        await alembic(url, "downgrade", "b12bf031a920")
        assert await snapshot() == before
        await target.dispose()
        await alembic(url, "upgrade", "head")
        await alembic(url, "check")
        assert await snapshot() == before
        async with target.begin() as db:
            await db.execute(
                insert(RosterOperation).values(
                    id=uuid4(),
                    actor_user_id=c.users["admin"].id,
                    request_id=uuid4(),
                    action="enable_operator",
                    payload_sha256="a" * 64,
                    result={},
                )
            )
        await target.dispose()
        result = await alembic(url, "downgrade", "b12bf031a920", success=False)
        assert (
            result.returncode != 0
            and "roster_downgrade_requires_archived_new_data" in result.stderr
        )
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
async def test_database_unique_numbers_and_students_and_schedule_guard(roster):
    from datetime import timedelta

    from huipi_cloud.modules.exams.schemas import SessionEdit

    c = roster
    r = await create(c, entries(c, 3))
    async with c.factory() as db:
        rows = list(
            (
                await db.execute(
                    select(RosterEntrySnapshot.__table__)
                    .where(RosterEntrySnapshot.roster_revision_id == UUID(r["id"]))
                    .order_by(RosterEntrySnapshot.exam_number)
                )
            ).mappings()
        )
        await db.rollback()
        # Isolate the exam-number unique key: third student is not otherwise in this draft.
        await db.execute(delete(RosterEntrySnapshot).where(RosterEntrySnapshot.id == rows[2]["id"]))
        with pytest.raises(DBAPIError) as err:
            await db.execute(
                insert(RosterEntrySnapshot).values(
                    **dict(rows[2], id=uuid4(), exam_number=rows[0]["exam_number"])
                )
            )
        assert getattr(err.value.orig, "sqlstate", None) == "23505"
        assert "uq_roster_entry_exam_number" in str(err.value.orig)
        await db.rollback()
        with pytest.raises(DBAPIError) as err:
            await db.execute(
                insert(RosterEntrySnapshot).values(
                    **dict(rows[0], id=uuid4(), exam_number="UNIQUE")
                )
            )
        assert getattr(err.value.orig, "sqlstate", None) == "23505"
        assert "uq_roster_entry_student" in str(err.value.orig)
        await db.rollback()
    s = c.session
    with pytest.raises(AccessError):
        await call(
            c,
            exams.edit_session,
            SessionEdit(
                request_id=uuid4(),
                exam_session_id=s["id"],
                expected_revision=s["revision"],
                title=s["title"],
                starts_at=s["starts_at"],
                ends_at=datetime.fromisoformat(s["ends_at"]) + timedelta(minutes=10),
                paper_policy="single",
            ),
        )
    assert (await view(c, r))["total"] == 3


@pytest.mark.anyio
async def test_two_incomplete_grants_do_not_allow_correction(roster):
    from huipi_cloud.modules.resource_security.models import (
        SecurityRolePermission,
        SecurityRoleVersion,
    )

    c = roster
    r = await create(c)
    await ready(c)
    f = await freeze(c, r)
    for code in ("manage", "correct"):
        rid = uuid4()
        async with c.factory() as db, db.begin():
            db.add(SecurityRoleVersion(id=rid, role_code="synthetic-" + uuid4().hex, revision=1))
            await db.flush()
            db.add(
                SecurityRolePermission(role_version_id=rid, permission_code="exam.roster." + code)
            )
            await db.flush()
            await db.execute(
                update(SecurityRoleVersion)
                .where(SecurityRoleVersion.id == rid)
                .values(sealed_at=func.clock_timestamp())
            )
        await grant(c, rid)
    with pytest.raises(AccessError):
        await call(
            c,
            rosters.correct_roster,
            RosterCorrection(**req(f).model_dump(), reason="不得拼接", entries=entries(c)),
            "a",
        )
    assert len((await view(c, f))["entries"]) == 2


@pytest.mark.anyio
async def test_freeze_correction_audit_rollback_and_commit_unknown_are_atomic(roster, monkeypatch):
    c = roster
    r = await create(c)
    await ready(c)
    f = await freeze(c, r)
    child = await call(
        c,
        rosters.correct_roster,
        RosterCorrection(**req(f).model_dump(), reason="合成更正", entries=entries(c, 1)),
    )
    request = req(child)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO roster_operations"):
            raise OperationalError("redacted", {}, Exception("synthetic audit"))

    async with c.factory() as db:
        event.listen(db.bind.sync_engine, "before_cursor_execute", fail)
        try:
            with pytest.raises(AccessError):
                await rosters.freeze_roster(db, c.principals["admin"], request)
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", fail)
    assert (await view(c, f))["roster"]["is_current"]
    assert (await view(c, child))["roster"]["status"] == "draft"
    async with c.factory() as db:
        original = db.commit

        async def lost():
            await original()
            raise OperationalError("redacted", {}, Exception("synthetic lost ACK"))

        monkeypatch.setattr(db, "commit", lost)
        with pytest.raises(AccessError):
            await rosters.freeze_roster(db, c.principals["admin"], request)
    response = await call(c, rosters.freeze_roster, request)
    assert response["status"] == "frozen" and response["is_current"]
    assert not (await view(c, f))["roster"]["is_current"]


@pytest.mark.anyio
@pytest.mark.parametrize("retire_first", [True, False])
async def test_retirement_freeze_serialization(roster, monkeypatch, retire_first):
    c = roster
    r = await create(c)
    await ready(c)
    entered, release = asyncio.Event(), asyncio.Event()

    async def retire():
        async with c.factory() as db:
            return await paper_service.retire_paper(db, c.principals["admin"], write(c.paper))

    if retire_first:
        original = paper_service.validate_write

        async def held(*a, **kw):
            result = await original(*a, **kw)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(paper_service, "validate_write", held)
        first = asyncio.create_task(retire())
        await entered.wait()
        second = asyncio.create_task(freeze(c, r))
    else:

        async def frozen():
            async with c.factory() as db:
                commit = db.commit

                async def held():
                    entered.set()
                    await release.wait()
                    await commit()

                monkeypatch.setattr(db, "commit", held)
                return await rosters.freeze_roster(db, c.principals["admin"], req(r))

        first = asyncio.create_task(frozen())
        await entered.wait()
        second = asyncio.create_task(retire())
    try:
        await wait_for_pg_lock(c.factory, "roster_revisions" if retire_first else "paper_versions")
    finally:
        release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], dict), results
    if retire_first:
        assert isinstance(results[1], AccessError)
    else:
        assert isinstance(results[1], dict), results
    data = await view(c, r)
    assert data["roster"]["status"] == ("draft" if retire_first else "frozen")
    assert data["total"] == 2
