"""Real PG/private S3, signed JWT, immutable source and teacher-score projections."""
# ruff: noqa: F401, F811

import asyncio
from decimal import Decimal
from io import BytesIO
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from sqlalchemy import func, select, text, update
from test_exam_grading import (
    SyntheticScoreProvider,
    auth_api,
    base,
    consume,
    detail,
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
from huipi_cloud.modules.exam_results import access, service
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.resource_security import service as security


async def allow(c, index=0, exporting=True):
    async with c.factory() as db:
        return await access.grant_class(
            db,
            c.principals["admin"],
            uuid4(),
            grant_id=UUID(c.mark_grant["grant_id"]),
            session_id=UUID(c.session["id"]),
            class_id=c.classes[index].id,
            export_allowed=exporting,
        )


@pytest.fixture
async def results(marking):
    c = marking
    async with c.factory() as db:
        link = (
            (await db.execute(select(g.parsing.K).where(g.parsing.K.c.job_id == c.jid)))
            .mappings()
            .one()
        )
        c.results_participation = str(link["participation_id"])
        entry = await g.row(db, access.E, link["roster_entry_id"])
        c.result_class_index = next(
            i for i, cls in enumerate(c.classes) if cls.id == entry["administrative_class_id"]
        )
    c.result_access = await allow(c, c.result_class_index)
    try:
        yield c
    finally:
        async with c.factory() as db, db.begin():
            await db.execute(text("TRUNCATE exam_result_class_access"))


def target(c, value):
    return next(s for s in value["students"] if s["participation_id"] == c.results_participation)


async def read(c, **params):
    return await c.client.get(
        f"/api/v1/exam-sessions/{c.session['id']}/results-preview", params=params
    )


async def score(c, index=0, status="accepted"):
    questions = (await c.client.get(f"/api/v1/exam-parsing-jobs/{c.jid}/grading-questions")).json()[
        "questions"
    ]
    c.wid = questions[index]["workspace_id"]
    d = await detail(c)
    node = next(n for n in d["nodes"] if n["page_index"] == index)
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/answer-review",
        json=dict(
            request_id=str(uuid4()),
            manual_attribution=True,
            expected_revision=0,
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
                reviewer_ref="teacher_" + c.users["a"].id.hex,
            ),
        ),
    )
    assert r.status_code == 200, r.text
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
        json=dict(request_id=str(uuid4()), expected_answer_revision=1),
    )
    assert r.status_code == 202, r.text
    assert await c.grade_worker.once()
    d = await detail(c)
    if status == "unreviewed" or status == "abstained":
        return d
    return await review(c, d, status)


async def review(c, d, status):
    payload = dict(
        schema_name="huipi.grading.review",
        schema_version="1.0",
        review_status=status,
        reviewer_ref="teacher_" + c.users["a"].id.hex,
    )
    if status == "revised":
        payload["criterion_reviews"] = [
            dict(
                criterion_id=r["criterion_id"],
                reviewed_points="3.00" if i == 0 else r["awarded_points"],
                **(
                    dict(
                        rationale="合成教师核对过程后修订",
                        evidence_references=r["evidence_references"],
                    )
                    if i == 0
                    else {}
                ),
            )
            for i, r in enumerate(d["proposal"]["criterion_scores"])
        ]
    if status in {"rejected", "regrading_required"}:
        payload["review_reason_codes"] = ["candidate_not_reliable"]
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/score-review",
        json=dict(request_id=str(uuid4()), expected_revision=len(d["history"]), payload=payload),
    )
    assert r.status_code == 200, r.text
    return await detail(c)


@pytest.mark.anyio
async def test_complete_partial_revised_and_no_candidate_total(results, tmp_path):
    c = results
    response = await read(c)
    assert response.status_code == 200, response.text
    first = target(c, response.json())
    assert first["total"] is None and first["subtotal"] is None
    assert first["required_count"] == 2 and first["attendance_status"] == "not_determined"
    await score(c, 0)
    value = target(c, (await read(c)).json())
    assert (
        value["reviewed_count"] == 1 and value["subtotal"] == "10.00" and value["total"] is None
    ), value
    await score(c, 1, "revised")
    response = await read(c)
    assert response.status_code == 200, response.text
    value = target(c, response.json())
    assert value["status"] == "complete" and value["total"] == "18.00"
    assert value["reviewed_count"] == value["required_count"] == 2
    assert [r["points"] for r in sorted(value["questions"], key=lambda q: q["number"])] == [
        "10.00",
        "8.00",
    ]
    assert all(q["review_id"] for q in value["questions"])
    assert response.json()["published"] is False
    exported = await c.client.get(f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx")
    assert exported.status_code == 200, exported.text
    (tmp_path / "preview.xlsx").write_bytes(exported.content)
    with ZipFile(BytesIO(exported.content)) as z:
        assert z.testzip() is None
        main = z.read("xl/worksheets/sheet1.xml")
        assert b"18.00" in main and b"10.00" in main and b"8.00" in main
        assert b"<f" not in main and b"sheetProtection" in main
        assert "未发布".encode() in main
        assert exported.headers["x-preview-sha256"].encode() in z.read("xl/worksheets/sheet3.xml")
    assert not any(
        k in response.text for k in ["object_key", "evidence_references", "response_content"]
    )
    import os
    from pathlib import Path

    if os.getenv("RESULT_EVIDENCE_DIR"):
        out = Path(os.environ["RESULT_EVIDENCE_DIR"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "verified-preview.xlsx").write_bytes(exported.content)
        (out / "verified-preview.json").write_text(response.text)


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["unreviewed", "rejected", "regrading_required", "abstained"])
async def test_nonfinal_states_never_count_as_zero_or_total(results, state):
    c = results
    if state == "abstained":
        from huipi_cloud.infrastructure.grading.provider import ProviderUsage, TextGradingResponse

        class Abstain(SyntheticScoreProvider):
            async def complete(self, **kwargs):
                return TextGradingResponse(
                    '{"schema_version":"1.0","outcome":"abstain",'
                    '"abstention_reason":"合成来源不足"}',
                    self.model_id,
                    1,
                    ProviderUsage(10, 10),
                )

        c.grade_worker.provider = Abstain()
    await score(c, 0, state)
    r = await read(c)
    assert r.status_code == 200, r.text
    value = target(c, r.json())
    assert value["total"] is None and value["subtotal"] is None and value["reviewed_count"] == 0
    expected = "awaiting_review" if state == "unreviewed" else state
    assert next(q for q in value["questions"] if q["number"] == 1)["status"] == expected


@pytest.mark.anyio
async def test_explicit_class_filter_same_names_and_unauthorized_export(results):
    c = results
    r = await read(c)
    assert r.status_code == 200, r.text
    assert len(r.json()["students"]) == 2  # class 1 holds N001 and N003
    assert {s["class_id"] for s in r.json()["students"]} == {
        str(c.classes[c.result_class_index].id)
    }
    assert (await read(c, class_id=str(c.classes[1 - c.result_class_index].id))).status_code == 404
    assert (
        await c.client.get(
            f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx",
            params=dict(class_id=str(c.classes[1 - c.result_class_index].id)),
        )
    ).status_code == 404
    await allow(c, 1 - c.result_class_index)
    r = await read(c)
    assert r.status_code == 200, r.text
    students = r.json()["students"]
    assert len(students) == 4 and len({s["student_id"] for s in students}) == 4
    assert len({s["student_name"] for s in students}) < len(students)
    for name in ["admin", "b"]:
        c.client.headers.update(c.headers(name))
        assert (await read(c)).status_code == 404
        assert (
            await c.client.get(f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx")
        ).status_code == 404
    c.client.headers.update(c.headers("a"))
    assert (
        await c.client.get(f"/api/v1/exam-sessions/{uuid4()}/results-preview")
    ).status_code == 404
    foreign = next(
        s for s in students if s["class_id"] == str(c.classes[1 - c.result_class_index].id)
    )
    # New teacher session loses class 2 again before direct participation lookup.
    async with c.factory() as db:
        access_id = await db.scalar(
            select(access.C.c.id).where(
                access.C.c.administrative_class_id == c.classes[1 - c.result_class_index].id
            )
        )
    async with c.factory() as db:
        await access.revoke_class(db, c.principals["admin"], uuid4(), access_id)
    assert (await read(c, participation_id=foreign["participation_id"])).status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("revocation", ["class", "grant", "deactivate"])
async def test_revocation_immediate_and_no_export_leak(results, revocation):
    c = results
    assert (await read(c)).status_code == 200
    if revocation == "class":
        async with c.factory() as db:
            await access.revoke_class(
                db, c.principals["admin"], uuid4(), UUID(c.result_access["class_access_id"])
            )
    elif revocation == "grant":
        async with c.factory() as db:
            await security.revoke_grant(
                db, c.principals["admin"], uuid4(), grant_id=UUID(c.mark_grant["grant_id"])
            )
    else:
        async with c.factory() as db, db.begin():
            await db.execute(
                update(g.parsing.User)
                .where(g.parsing.User.id == c.users["a"].id)
                .values(is_active=False)
            )
    assert (await read(c)).status_code == (403 if revocation == "deactivate" else 404)
    assert (
        await c.client.get(f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx")
    ).status_code == (403 if revocation == "deactivate" else 404)


@pytest.mark.anyio
async def test_latest_rejection_overrides_previous_accepted_score(results):
    c = results
    d = await score(c, 0)
    await review(c, d, "rejected")
    value = target(c, (await read(c)).json())
    q = next(q for q in value["questions"] if q["number"] == 1)
    assert q["status"] == "rejected" and q["points"] is None
    assert q["review_revision"] == 2 and value["subtotal"] is None and value["total"] is None


@pytest.mark.anyio
async def test_answer_change_invalidates_scores(results):
    from test_exam_grading import confirm

    c = results
    await score(c, 0)
    assert (await confirm(c)).status_code == 200
    value = target(c, (await read(c)).json())
    assert (
        value["status"] == "source_stale" and value["total"] is None and value["subtotal"] is None
    )


@pytest.mark.anyio
async def test_class_access_audit_atomicity_and_database_immutability(results):
    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError

    c = results
    async with c.factory() as db:
        before = await db.scalar(select(func.count()).select_from(access.C))
    engine = c.factory.kw["bind"].sync_engine

    def fail(conn, cursor, statement, params, ctx, many):
        if statement.startswith("INSERT INTO security_audit_events"):
            raise RuntimeError("synthetic audit failure")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="synthetic audit failure"):
            await allow(c, 1 - c.result_class_index)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(access.C)) == before
    with pytest.raises(DBAPIError):
        async with c.factory() as db, db.begin():
            await db.execute(update(access.C).values(export_allowed=False))
    assert (await read(c)).status_code == 200


@pytest.mark.anyio
async def test_export_separate_class_capability_and_request_identity(results):
    c = results
    async with c.factory() as db:
        await access.revoke_class(
            db, c.principals["admin"], uuid4(), UUID(c.result_access["class_access_id"])
        )
    await allow(c, c.result_class_index, False)
    assert (await read(c)).status_code == 200
    r = await c.client.get(f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx")
    assert r.status_code == 404 and not r.content.startswith(b"PK")
    old = dict(c.client.headers)
    c.client.headers.clear()
    try:
        assert (await read(c)).status_code == 401
    finally:
        c.client.headers.update(old)
    async with c.factory() as db:
        with pytest.raises(AccessError):
            await access.grant_class(
                db,
                c.principals["a"],
                uuid4(),
                grant_id=UUID(c.mark_grant["grant_id"]),
                session_id=UUID(c.session["id"]),
                class_id=c.classes[1 - c.result_class_index].id,
            )


@pytest.mark.anyio
async def test_source_bytes_tampering_fail_closed(results):
    c = results
    async with c.factory() as db:
        ca = (await db.execute(select(g.parsing.CA))).mappings().one()
    await c.storage.upload_fileobj(
        BytesIO(b"corrupt-test-only"), ca["canonical_object_key"], "application/json"
    )
    r = await read(c)
    assert r.status_code in {422, 503} and "students" not in r.json()


@pytest.mark.anyio
async def test_concurrent_review_export_is_one_consistent_snapshot(results, monkeypatch):
    c = results
    await score(c, 0)
    d = await score(c, 1)
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.question_result
    paused = False

    async def pause(*args, **kwargs):
        nonlocal paused
        result = await original(*args, **kwargs)
        if not paused:
            paused = True
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(service, "question_result", pause)
    reading = asyncio.create_task(
        c.client.get(f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx")
    )
    await asyncio.wait_for(entered.wait(), 10)
    writing = asyncio.create_task(review(c, d, "revised"))
    try:
        async with asyncio.timeout(10):
            while True:
                async with c.factory() as db:
                    waiting = await db.scalar(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE "
                            "datname=current_database() AND wait_event_type='Lock' "
                            "AND pid<>pg_backend_pid())"
                        )
                    )
                if waiting:
                    break
                await asyncio.sleep(0.01)
        assert not writing.done()
    finally:
        release.set()
    exported = await reading
    assert exported.status_code == 200, exported.text
    with ZipFile(BytesIO(exported.content)) as z:
        sheet = z.read("xl/worksheets/sheet1.xml")
        assert b"20.00" in sheet and b"18.00" not in sheet
    await writing
    after = target(c, (await read(c)).json())
    assert after["total"] == "18.00"


@pytest.mark.anyio
async def test_ab_papers_have_separate_mapping_sets_and_no_inferred_identity(roster):
    from test_exam_rosters import create, entries, freeze, ready
    from test_exam_sessions import bind_request, call, new_session
    from test_paper_versions import create as snapshot_paper
    from test_paper_versions import lock

    from huipi_cloud.modules.exam_grading.permissions import MARK_OPERATOR_ROLE
    from huipi_cloud.modules.exams import service as exams
    from huipi_cloud.modules.papers.schemas import SnapshotCreate
    from huipi_cloud.modules.resource_security.models import SecurityScope

    c = roster
    c.session = await new_session(c, policy="ab", offset=1)
    bound = await call(c, exams.bind_paper, bind_request(c, c.session))
    c.binding, c.session = bound["binding"], bound["session"]
    response = await c.client.post(
        "/api/v1/assignments", json=dict(title="合成B卷来源", subject="数学", grade_level="高二")
    )
    aid = response.json()["id"]
    question = await c.client.post(
        f"/api/v1/assignments/{aid}/questions",
        json=dict(
            question_number=1,
            question_type="short_answer",
            stem="合成B卷不同题目",
            max_score="6.00",
        ),
    )
    qid = question.json()["id"]
    assert (
        await c.client.put(
            f"/api/v1/questions/{qid}/answer-key", json=dict(answer_content="合成答案")
        )
    ).status_code == 200
    assert (
        await c.client.put(
            f"/api/v1/questions/{qid}/rubric",
            json=dict(criteria=[dict(description="合成评分项", points="6.00", sort_order=1)]),
        )
    ).status_code == 200
    assert (await c.client.post(f"/api/v1/assignments/{aid}/publish")).status_code == 200
    b = await lock(
        c,
        await snapshot_paper(
            c, request=SnapshotCreate(request_id=uuid4(), source_assignment_id=aid, title="B卷")
        ),
    )
    bound = await call(
        c,
        exams.bind_paper,
        bind_request(c, c.session, variant_code="B").model_copy(
            update={"paper_version_id": UUID(b["paper_version_id"])}
        ),
    )
    c.session = bound["session"]
    values = entries(c)
    values[1]["session_paper_id"] = bound["binding"]["id"]
    revision = await create(c, values)
    await ready(c)
    await freeze(c, revision)
    async with c.factory() as db:
        await g.enable_operator(
            db, c.users["admin"].id, c.users["admin"].id, uuid4(), "synthetic result deployment"
        )
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        c.mark_grant = await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=UUID(MARK_OPERATOR_ROLE),
            scope_id=scope,
        )
    try:
        await allow(c, 0)
        await allow(c, 1)
        r = await read(c)
        assert r.status_code == 200, r.text
        a, b = r.json()["students"]
        assert a["variant"] == "A" and b["variant"] == "B"
        assert a["required_count"] == 2 and b["required_count"] == 1
        assert a["max_points"] == "20.00" and b["max_points"] == "6.00"
        assert not {q["mapping_id"] for q in a["questions"]} & {
            q["mapping_id"] for q in b["questions"]
        }
        assert a["total"] is None and b["total"] is None
        exported = await c.client.get(
            f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx"
        )
        assert exported.status_code == 200
        with ZipFile(BytesIO(exported.content)) as z:
            data = z.read("xl/worksheets/sheet1.xml").decode()
            assert "A卷·第1题" in data and "B卷·第1题" in data
    finally:
        async with c.factory() as db, db.begin():
            await db.execute(text("TRUNCATE exam_result_class_access"))


@pytest.mark.anyio
async def test_new_migration_roundtrip_preserves_exam_and_review_facts(results):
    import hashlib
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    c = results
    await score(c, 0)
    url = c.factory.kw["bind"].url
    assert url.database.endswith("_test")
    env = dict(os.environ, DATABASE_URL=url.render_as_string(hide_password=False))
    root = Path(__file__).parents[2]

    async def alembic(*args):
        return await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
        )

    blocked = await alembic("downgrade", "b14c52b3f014")
    assert blocked.returncode != 0 and "paper_creator_facts_prevent_downgrade" in blocked.stderr
    # Isolated TEST database only: remove the later creation-policy fixture metadata
    # after proving its downgrade guard. Keep all paper/exam/review facts and original
    # operator grants, then independently prove the older results migration guard below.
    from huipi_cloud.modules.papers.permissions import PAPER_AUTHOR_ROLE

    async with c.factory() as db, db.begin():
        await db.execute(text("TRUNCATE paper_creator_authorities"))
        await db.execute(
            text("DELETE FROM security_grants WHERE role_version_id=:role"),
            {"role": PAPER_AUTHOR_ROLE},
        )
    blocked = await alembic("downgrade", "b14c52b3f014")
    assert blocked.returncode != 0 and "prevent destructive downgrade" in blocked.stderr
    # Test-only cleanup of this new slice; never a production rollback procedure.
    async with c.factory() as db, db.begin():
        await db.execute(text("TRUNCATE exam_result_class_access"))
    tables = [
        "users",
        "assignments",
        "questions",
        "submissions",
        "submission_files",
        "parsed_artifacts",
        "canonical_artifacts",
        "paper_versions",
        "paper_question_mappings",
        "exam_events",
        "exam_sessions",
        "session_papers",
        "exam_participations",
        "roster_revisions",
        "roster_entry_snapshots",
        "answer_scripts",
        "answer_script_versions",
        "scan_files",
        "scan_pages",
        "exam_parsing_jobs",
        "exam_submission_links",
        "answer_review_decisions",
        "grade_input_artifacts",
        "grade_proposal_artifacts",
        "grade_review_revisions",
        "exam_grading_workspaces",
        "exam_scoring_jobs",
    ]

    async def fingerprint():
        values = {}
        async with c.factory() as db:
            for table in tables:
                data = (
                    (
                        await db.execute(
                            text(
                                f'SELECT to_jsonb(t) AS row FROM "{table}" t '
                                "ORDER BY to_jsonb(t)::text"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                values[table] = dict(
                    count=len(data),
                    sha256=hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
                )
        return values

    before = await fingerprint()
    assert before["grade_review_revisions"]["count"] > 0 and before["exam_sessions"]["count"] > 0
    for args in [("downgrade", "b14c52b3f014"), ("upgrade", "head"), ("check",)]:
        result = await alembic(*args)
        assert result.returncode == 0, result.stderr[-3000:]
    assert await fingerprint() == before
    if os.getenv("RESULT_EVIDENCE_DIR"):
        out = Path(os.environ["RESULT_EVIDENCE_DIR"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "migration-history.json").write_text(
            json.dumps(
                dict(
                    before=before,
                    after=await fingerprint(),
                    old_head="b14c52b3f014",
                    new_head="b15a60c4f015",
                    downgrade_with_facts_refused=True,
                ),
                indent=2,
            )
        )


@pytest.mark.anyio
async def test_single_grant_class_authority_cannot_be_spliced(results):
    from datetime import UTC, datetime

    from sqlalchemy import insert

    from huipi_cloud.modules.resource_security.models import (
        SecurityRolePermission,
        SecurityRoleVersion,
        SecurityScope,
    )

    c = results
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    for codes in [
        access.REQUIRED - {"exam.roster.read"},
        {"exam.roster.read", "exam.score.review"},
    ]:
        identifier = uuid4()
        async with c.factory() as db, db.begin():
            db.add(
                SecurityRoleVersion(
                    id=identifier, role_code="result_test_" + identifier.hex, revision=1
                )
            )
            await db.flush()
            db.add_all(
                SecurityRolePermission(role_version_id=identifier, permission_code=code)
                for code in codes
            )
            await db.flush()
            await db.execute(
                update(SecurityRoleVersion)
                .where(SecurityRoleVersion.id == identifier)
                .values(sealed_at=datetime.now(UTC))
            )
        async with c.factory() as db:
            grant = await security.grant_role(
                db,
                c.principals["admin"],
                uuid4(),
                user_id=c.users["b"].id,
                role_version_id=identifier,
                scope_id=scope,
            )
        async with c.factory() as db, db.begin():
            await db.execute(
                insert(access.C).values(
                    id=uuid4(),
                    grant_id=UUID(grant["grant_id"]),
                    exam_session_id=UUID(c.session["id"]),
                    administrative_class_id=c.classes[c.result_class_index].id,
                    export_allowed=True,
                    created_by_user_id=c.users["admin"].id,
                )
            )
    c.client.headers.update(c.headers("b"))
    assert (await read(c)).status_code == 404
    r = await c.client.get(f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx")
    assert r.status_code == 404 and not r.content.startswith(b"PK")


@pytest.mark.anyio
async def test_database_fault_and_export_limit_return_no_student_bytes(results, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    c = results
    monkeypatch.setattr(service, "MAX_EXPORT_STUDENTS", 1)
    r = await c.client.get(f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx")
    assert r.status_code == 413 and not r.content.startswith(b"PK")
    assert (await read(c, limit=51)).status_code == 422
    engine = c.factory.kw["bind"].sync_engine

    def fail(conn, cursor, statement, params, ctx, many):
        if statement.startswith("SET TRANSACTION ISOLATION LEVEL"):
            raise OperationalError("synthetic database failure", {}, Exception("unavailable"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        r = await read(c)
        assert r.status_code == 503 and "students" not in r.json()
    finally:
        event.remove(engine, "before_cursor_execute", fail)


@pytest.mark.anyio
async def test_mixed_read_export_classes_require_explicit_export_scope(results):
    c = results
    await allow(c, 1 - c.result_class_index, exporting=False)
    assert len((await read(c)).json()["students"]) == 4
    path = f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx"
    r = await c.client.get(path)
    assert r.status_code == 403 and not r.content.startswith(b"PK")
    r = await c.client.get(path, params=dict(class_id=str(c.classes[c.result_class_index].id)))
    assert r.status_code == 200 and r.content.startswith(b"PK")
    r = await c.client.get(path, params=dict(class_id=str(c.classes[1 - c.result_class_index].id)))
    assert r.status_code == 404 and not r.content.startswith(b"PK")
