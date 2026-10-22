"""Bounded roster benchmark against real PostgreSQL; no paid providers.

ROSTER_BENCHMARK_OUTPUT writes aggregate JSONL, never names or database URLs.
Run with ROSTER_BENCHMARK_REPEATS=2 to compare repeated measurements.
"""

import asyncio
import json
import os
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from test_exam_rosters import freeze, ready, req, view
from test_exam_rosters import roster as roster
from test_exam_sessions import bind_request, call
from test_exam_sessions import exam as exam
from test_paper_versions import create as new_paper
from test_paper_versions import lock
from test_paper_versions import papers as papers
from test_teacher_authorization import auth_api as auth_api

from huipi_cloud.modules.exam_rosters import service as rosters
from huipi_cloud.modules.exam_rosters.models import ExamParticipation, RosterEntrySnapshot
from huipi_cloud.modules.exam_rosters.schemas import RosterCorrection, RosterCreate, RosterReplace
from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.exams.schemas import SessionEdit
from huipi_cloud.modules.organization.models import AdministrativeClass, ClassEnrollment, Student


def rss_bytes():
    """Linux process resident memory, including native libraries; not allocation attribution."""
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("benchmark_requires_linux_proc")


async def seed(c, count, classes, ab=False):
    classrooms = list(c.classes)
    async with c.factory() as db, db.begin():
        extra = [
            dict(
                id=uuid4(),
                grade_cohort_id=classrooms[0].grade_cohort_id,
                code=uuid4().hex,
                display_name="合成性能班",
            )
            for _ in range(classes - len(classrooms))
        ]
        if extra:
            await db.execute(insert(AdministrativeClass.__table__), extra)
        class_ids = [v.id for v in classrooms] + [v["id"] for v in extra]
        students = [
            dict(id=uuid4(), school_student_code="B" + uuid4().hex, name="合成同名学生")
            for _ in range(count)
        ]
        await db.execute(insert(Student.__table__), students)
        await db.execute(
            insert(ClassEnrollment.__table__),
            [
                dict(
                    id=uuid4(),
                    student_id=s["id"],
                    administrative_class_id=class_ids[i % classes],
                    valid_from=date(2026, 9, 1),
                )
                for i, s in enumerate(students)
            ],
        )
    bindings = [c.binding["id"]]
    if ab:
        s = c.session
        c.session = await call(
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
        paper = await lock(c, await new_paper(c, title="合成性能B卷"))
        bound = await call(
            c,
            exams.bind_paper,
            bind_request(c, c.session, variant_code="B").model_copy(
                update={"paper_version_id": UUID(paper["paper_version_id"])}
            ),
        )
        c.session = bound["session"]
        bindings.append(bound["binding"]["id"])
    return [
        dict(
            student_id=s["id"], exam_number=f"B{i:05}", session_paper_id=bindings[i % len(bindings)]
        )
        for i, s in enumerate(students)
    ]


async def measured(c, function, request):
    """Count application wire executions only; monitor SQL has a separate engine.

    Cursor wall time includes network, PostgreSQL work, trigger internals and lock waits.
    It cannot be interpreted as trigger CPU or trigger-internal statement count.
    """
    record = dict(
        assembly_seconds=0.0,
        max_bind_parameters=0,
        insert_executemany=False,
        sql_executions=0,
        cursor_seconds=0.0,
        commit_seconds=0.0,
        lock_wait_samples=0,
        sampled_lock_seconds=0.0,
        outcome="failed",
    )
    rss_start = record["rss_peak_bytes"] = rss_bytes()
    monitor_engine = create_async_engine(c.factory.kw["bind"].url)
    pid, stopped = None, asyncio.Event()
    cursor_start = 0.0

    def began(session, transaction, connection):
        nonlocal pid
        pid = connection.connection.driver_connection.get_server_pid()

    def before(connection, cursor, statement, parameters, context, many):
        nonlocal cursor_start
        record["sql_executions"] += 1
        record["max_bind_parameters"] = max(
            record["max_bind_parameters"], len(parameters[0]) if many else len(parameters)
        )
        if statement.startswith("INSERT INTO roster_entry_snapshots") and many:
            record["insert_executemany"] = True
        cursor_start = time.perf_counter()

    def after(*args):
        record["cursor_seconds"] += time.perf_counter() - cursor_start

    async def sample():
        async with monitor_engine.connect() as connection:
            previous = time.perf_counter()
            while not stopped.is_set():
                record["rss_peak_bytes"] = max(record["rss_peak_bytes"], rss_bytes())
                now = time.perf_counter()
                if pid is not None:
                    waiting = await connection.scalar(
                        text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"),
                        {"pid": pid},
                    )
                    if waiting:
                        record["lock_wait_samples"] += 1
                        record["sampled_lock_seconds"] += now - previous
                    await connection.commit()  # fresh stats snapshot on the next sample
                previous = now
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=0.02)
                except TimeoutError:
                    pass

    original_assembly = rosters.snapshot_rows

    def assembly(*args):
        started = time.perf_counter()
        try:
            return original_assembly(*args)
        finally:
            record["assembly_seconds"] += time.perf_counter() - started

    async with c.factory() as db:
        engine = db.bind.sync_engine
        event.listen(db.sync_session, "after_begin", began)
        event.listen(engine, "before_cursor_execute", before)
        event.listen(engine, "after_cursor_execute", after)
        original_commit = db.commit

        async def commit():
            start = time.perf_counter()
            try:
                return await original_commit()
            finally:
                record["commit_seconds"] += time.perf_counter() - start

        db.commit = commit
        sampler = asyncio.create_task(sample())
        start = time.perf_counter()
        try:
            with patch.object(rosters, "snapshot_rows", assembly):
                value = await function(db, c.principals["admin"], request)
            record["outcome"] = "committed"
        finally:
            record["total_seconds"] = time.perf_counter() - start
            stopped.set()
            event.remove(engine, "before_cursor_execute", before)
            event.remove(engine, "after_cursor_execute", after)
            await sampler
            await monitor_engine.dispose()
    record["rss_peak_delta_bytes"] = record["rss_peak_bytes"] - rss_start
    record["non_cursor_non_commit_seconds"] = max(
        0, record["total_seconds"] - record["cursor_seconds"] - record["commit_seconds"]
    )
    return value, record


@pytest.mark.anyio
@pytest.mark.parametrize("trial", range(int(os.getenv("ROSTER_BENCHMARK_REPEATS", "1"))))
@pytest.mark.parametrize(
    "count,classes,ab", [(100, 2, False), (400, 8, False), (2000, 8, False), (2000, 8, True)]
)
async def test_benchmark_full_roster_lifecycle(roster, count, classes, ab, trial):
    c = roster
    selections = await seed(c, count, classes, ab)
    await ready(c)
    records = []
    request = RosterCreate(
        request_id=uuid4(),
        exam_session_id=c.session["id"],
        business_date="2026-11-01",
        reason="合成性能基准",
        entries=selections,
    )
    value, timing = await measured(c, rosters.create_roster, request)
    records.append(dict(operation="create", repeat=trial, **timing))
    replacement = [dict(e, exam_number="R" + e["exam_number"]) for e in selections]
    value, timing = await measured(
        c,
        rosters.replace_entries,
        RosterReplace(**req(value).model_dump(), entries=replacement),
    )
    records.append(dict(operation="replace", repeat=trial, **timing))
    frozen, timing = await measured(c, rosters.freeze_roster, req(value))
    records.append(dict(operation="freeze", repeat=trial, **timing))
    value, timing = await measured(
        c,
        rosters.correct_roster,
        RosterCorrection(**req(frozen).model_dump(), reason="合成性能更正", entries=selections),
    )
    records.append(dict(operation="correct", repeat=trial, **timing))
    value = await freeze(c, value)
    # Full service pagination, not just a count: every snapshot and stable identity checked.
    actual = []
    for offset in range(0, count, 100):
        page = await view(c, value, offset=offset, limit=100)
        actual.extend(page["entries"])
    assert {r["student_id"] for r in actual} == {str(e["student_id"]) for e in selections}
    assert {r["exam_number"] for r in actual} == {e["exam_number"] for e in selections}
    assert len({r["administrative_class_id"] for r in actual}) == classes
    assert {r["session_paper_id"] for r in actual} == {e["session_paper_id"] for e in selections}
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ExamParticipation)) == count
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RosterEntrySnapshot)
                .where(RosterEntrySnapshot.roster_revision_id == UUID(value["id"]))
            )
            == count
        )
        version = await db.scalar(text("SHOW server_version"))
    result = dict(
        students=count,
        classes=classes,
        ab=ab,
        trial=trial,
        postgres=version,
        records=records,
        label=os.getenv("ROSTER_BENCHMARK_LABEL", "unspecified"),
    )
    if output := os.getenv("ROSTER_BENCHMARK_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(json.dumps(result) + "\n")
    print("ROSTER_BENCHMARK", json.dumps(result))
    assert all(r["sql_executions"] <= 256 for r in records)
    assert all(r["max_bind_parameters"] <= 5000 and not r["insert_executemany"] for r in records)


@pytest.mark.anyio
async def test_server_trigger_profile_on_real_bulk_insert(roster, monkeypatch):
    """Separate server-side EXPLAIN evidence; never disable or bypass row guards."""
    c = roster
    selections = await seed(c, 2000, 8)
    original = rosters.insert_batches
    triggers = {}
    execution_seconds = 0.0

    async def explain(db, table, values):
        nonlocal execution_seconds
        if table is not rosters.E:
            return await original(db, table, values)
        connection = await db.connection()
        for start in range(0, len(values), rosters.WRITE_BATCH_SIZE):
            compiled = (
                insert(table)
                .values(values[start : start + rosters.WRITE_BATCH_SIZE])
                .compile(dialect=db.bind.dialect)
            )
            result = await connection.exec_driver_sql(
                "EXPLAIN (ANALYZE, FORMAT JSON) " + str(compiled),
                tuple(compiled.params[k] for k in compiled.positiontup),
            )
            document = result.scalar_one()
            if isinstance(document, str):
                document = json.loads(document)
            plan = document[0]
            execution_seconds += plan["Execution Time"] / 1000
            for trigger in plan["Triggers"]:
                metric = triggers.setdefault(trigger["Trigger Name"], dict(calls=0, seconds=0.0))
                metric["calls"] += trigger["Calls"]
                metric["seconds"] += trigger["Time"] / 1000

    monkeypatch.setattr(rosters, "insert_batches", explain)
    value = await call(
        c,
        rosters.create_roster,
        RosterCreate(
            request_id=uuid4(),
            exam_session_id=c.session["id"],
            business_date="2026-11-01",
            reason="合成数据库触发器剖析",
            entries=selections,
        ),
    )
    assert (await view(c, value))["total"] == 2000
    assert triggers["protect_roster"]["calls"] == 2000
    profile = dict(
        students=2000,
        execution_seconds=execution_seconds,
        triggers=triggers,
        internal_sql_count="not_measured",
        source="PostgreSQL EXPLAIN ANALYZE",
    )
    if output := os.getenv("ROSTER_TRIGGER_PROFILE_OUTPUT"):
        Path(output).write_text(json.dumps(profile))
    print("ROSTER_TRIGGER_PROFILE", json.dumps(profile))
