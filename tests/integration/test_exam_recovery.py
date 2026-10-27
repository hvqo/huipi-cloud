"""Live exceptions over real, isolated PostgreSQL and private S3; no inference calls."""
# ruff: noqa: F401,F811

from io import BytesIO
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_marking_tasks import (
    assign,
    auth_api,
    base,
    claim,
    collaborative,
    consume,
    exam,
    marking,
    papers,
    parse_context,
    roster,
    scan_context,
    script_context,
)
from test_scan_intake import pdf_bytes

from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.exam_grading import service as grading
from huipi_cloud.modules.exam_recovery import service
from huipi_cloud.modules.identity.models import User
from huipi_cloud.modules.resource_security import service as security


async def view(c, actor="a", suffix=""):
    return await c.client.get(
        f"/api/v1/exam-sessions/{c.session['id']}/exceptions{suffix}", headers=c.headers(actor)
    )


@pytest.mark.anyio
async def test_live_projection_counts_filters_and_private_fields(marking):
    c = marking
    r = await view(c)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["counts"]["answer_unconfirmed"] == 2
    assert d["total"] == sum(d["counts"].values())
    assert not any(
        word in r.text
        for word in [
            "object_key",
            "file_key",
            "bucket",
            "student_name",
            "answer_key",
            "access_token",
        ]
    )
    r = await view(c, suffix="?kind=answer_unconfirmed")
    assert r.status_code == 200 and r.json()["total"] == 2
    assert {i["kind"] for i in r.json()["items"]} == {"answer_unconfirmed"}
    assert (await view(c, suffix="?offset=100")).json()["items"] == []
    assert (await view(c, suffix="?kind=arbitrary_sql")).status_code == 422
    sessions = await c.client.get("/api/v1/recovery-sessions", headers=c.headers("a"))
    assert c.session["id"] in [i["id"] for i in sessions.json()["items"]]


@pytest.mark.anyio
async def test_current_grants_scope_legacy_role_and_database_failure(marking, monkeypatch):
    c = marking
    for actor in ["b", "reader"]:
        assert (await view(c, actor)).status_code == 404
    r = await c.client.get(
        f"/api/v1/exam-sessions/{c.session['id']}/exceptions",
        headers=c.headers("b", role="admin", is_admin=True),
    )
    assert r.status_code == 404
    assert (
        await c.client.get(f"/api/v1/exam-sessions/{uuid4()}/exceptions", headers=c.headers("a"))
    ).status_code == 404
    assert (
        await c.client.get(
            f"/api/v1/exam-sessions/{c.session['id']}/exceptions", headers={"Authorization": ""}
        )
    ).status_code == 401
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.mark_grant["grant_id"])
        )
    # Actor retains legitimate script/parse roles, but no longer has full grading projection.
    r = await view(c)
    assert r.status_code == 200 and "answer_unconfirmed" not in r.json()["counts"]


@pytest.mark.anyio
async def test_failed_upload_keeps_original_request_for_safe_retry(marking, monkeypatch):
    c = marking
    rid = str(uuid4())
    original = c.storage.upload_path

    async def failed(path, key, content_type):
        if "/exam-scans/" in key:
            raise StorageUnavailableError("synthetic storage interruption")
        return await original(path, key, content_type)

    monkeypatch.setattr(c.storage, "upload_path", failed)
    response = await c.client.post(
        f"/api/v1/scan-batches/{c.batch['id']}/files",
        headers=c.headers("a"),
        data={"request_id": rid},
        files={
            "file": ("retry.pdf", pdf_bytes(label="Synthetic recovery upload"), "application/pdf")
        },
    )
    assert response.status_code == 503, response.text
    d = (await view(c)).json()
    item = next(i for i in d["items"] if i["kind"] == "upload_failed")
    assert item["retry_request_id"] == rid
    monkeypatch.setattr(c.storage, "upload_path", original)
    r = await c.client.post(
        f"/api/v1/scan-batches/{c.batch['id']}/files",
        headers=c.headers("a"),
        data={"request_id": rid},
        files={
            "file": ("retry.pdf", pdf_bytes(label="Synthetic recovery upload"), "application/pdf")
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["id"] == item["file_id"] and r.json()["attempt_number"] == 2
    assert "upload_failed" not in (await view(c)).json()["counts"]


@pytest.mark.anyio
async def test_task_only_teacher_projection_never_reveals_unassigned_resources(collaborative):
    c = collaborative
    assert (await view(c, "marker_a")).status_code == 404
    tid = (await assign(c)).json()["task_ids"][0]
    d = (await view(c, "marker_a")).json()
    assert len(d["items"]) == 1
    assert d["items"][0]["workspace_id"] == c.wid and d["items"][0]["task_id"] == tid
    assert not d["capabilities"]["files"] and not d["capabilities"]["scripts"]
    assert d["results"] == {} and set(d["counts"]) == {"answer_unconfirmed"}
    assert (await view(c, "marker_b")).status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["abstain", "invalid_total"])
async def test_real_protocol_failure_and_abstention_remain_pending_not_zero(marking, mode):
    import json

    from test_exam_grading import confirm

    from huipi_cloud.infrastructure.grading.provider import ProviderUsage, TextGradingResponse

    c = marking
    assert (await confirm(c)).status_code == 200
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
        json=dict(request_id=str(uuid4()), expected_answer_revision=1),
    )
    assert r.status_code == 202
    original = c.grade_worker.provider.complete

    async def altered(**kwargs):
        response = await original(**kwargs)
        body = json.loads(response.content)
        if mode == "abstain":
            body = dict(
                schema_version="1.0",
                outcome="abstain",
                abstention_reason="Synthetic unclear evidence",
                reason_codes=["insufficient_evidence"],
            )
        else:
            body["total_score"] = "0.00"
        return TextGradingResponse(json.dumps(body), "synthetic-test-only", 1, ProviderUsage(1, 1))

    c.grade_worker.provider.complete = altered
    assert await c.grade_worker.once()
    d = (await view(c)).json()
    expected = "scoring_abstained" if mode == "abstain" else "scoring_failed"
    assert d["counts"][expected] == 1
    assert not any("total_score" in item for item in d["items"])


@pytest.mark.anyio
async def test_incomplete_confirm_recover_parser_and_preserve_failed_history(parse_context):
    from test_answer_scripts import confirmation
    from test_exam_parsing import start

    from huipi_cloud.modules.parsing.errors import PermanentParsingError

    c = parse_context
    change = dict(
        request_id=str(uuid4()),
        expected_revision=c.answer["revision"],
        action="save_draft",
        page_ids=c.page_ids[:1],
        completeness="incomplete",
        reason="合成缺页",
    )
    r = await c.client.post(f"/api/v1/answer-scripts/{c.answer['script_id']}/versions", json=change)
    assert r.status_code == 200
    c.answer = r.json()
    assert (await view(c)).json()["counts"]["identity_pending"] == 1
    change = confirmation(c, c.answer)
    change["completeness"] = "incomplete"
    r = await c.client.post(f"/api/v1/answer-scripts/{c.answer['script_id']}/versions", json=change)
    assert r.status_code == 200
    c.answer = r.json()
    assert (await view(c)).json()["counts"]["incomplete_script"] == 1
    assert (await start(c)).status_code == 409
    change = confirmation(c, c.answer)
    r = await c.client.post(f"/api/v1/answer-scripts/{c.answer['script_id']}/versions", json=change)
    assert r.status_code == 200
    c.answer = r.json()
    assert (await start(c)).status_code == 202
    original = c.parser.execute

    async def broken(task):
        raise PermanentParsingError("corrupt_input")

    c.parser.execute = broken
    await consume(c)
    d = (await view(c)).json()
    assert d["counts"]["parsing_failed"] == 1
    assert "文件损坏" in next(i["reason"] for i in d["items"] if i["kind"] == "parsing_failed")
    async with c.factory() as db:
        old = (await db.execute(select(service.J))).mappings().one()
    # Explicit version correction, not revival of the failed job or overwritten evidence.
    r = await c.client.post(
        f"/api/v1/answer-scripts/{c.answer['script_id']}/versions", json=confirmation(c, c.answer)
    )
    assert r.status_code == 200
    c.answer = r.json()
    c.parser.execute = original
    assert (await start(c)).status_code == 202
    await consume(c)
    d = (await view(c)).json()
    assert "parsing_failed" not in d["counts"] and d["results"]["parsed_scripts"] == 1
    async with c.factory() as db:
        assert (
            await db.execute(select(service.J).where(service.J.c.id == old["id"]))
        ).mappings().one()["status"] == "failed"
        assert await db.scalar(select(func.count()).select_from(service.J)) == 2


@pytest.mark.anyio
async def test_task_and_old_score_invalidated_after_source_change(collaborative):
    from test_paper_versions import write

    from huipi_cloud.modules.papers.service import retire_paper

    c = collaborative
    tid = (await assign(c)).json()["task_ids"][0]
    async with c.factory() as db:
        await retire_paper(db, c.principals["admin"], write(c.paper))
    d = (await view(c)).json()
    assert d["counts"]["source_changed"] == 1 and d["counts"]["task_invalidated"] == 1
    assert d["results"]["confirmed_complete_scripts"] == 0 and d["results"]["parsed_scripts"] == 0
    d = (await view(c, "marker_a")).json()
    assert d["counts"]["task_invalidated"] == 1 and len(d["items"]) <= 2
    assert {r["task_id"] for r in d["items"]} == {tid}


@pytest.mark.anyio
async def test_duplicate_preview_is_only_a_manual_warning_and_summary_is_bounded(marking):
    c = marking
    r = await c.client.post(
        f"/api/v1/exam-sessions/{c.session['id']}/scan-batches",
        json=dict(request_id=str(uuid4()), title="合成重复扫描核验"),
    )
    assert r.status_code == 201
    # Same PDF in a different batch is a real source fact, not automatically discarded.

    r = await c.client.post(
        f"/api/v1/scan-batches/{r.json()['id']}/files",
        data=dict(request_id=str(uuid4())),
        files={"file": ("repeat.pdf", pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 201, r.text
    d = (await view(c)).json()
    assert d["counts"].get("duplicate_page_check", 0) > 0
    assert all(
        i["state"] == "manual_review" for i in d["items"] if i["kind"] == "duplicate_page_check"
    )
    summary = (await view(c, suffix="?summary=true")).json()
    assert summary["items"] == [] and not summary["has_next"] and summary["counts"] == d["counts"]
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(service.P)) == 8


@pytest.mark.anyio
async def test_database_projection_failure_is_fail_closed_without_raw_data(
    marking, postgres_engine
):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    c = marking

    def failed(connection, cursor, statement, *args):
        if "UNION ALL" in statement:
            raise OperationalError("synthetic projection", {}, Exception("unavailable"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", failed)
    try:
        r = await view(c)
        assert r.status_code == 503
        assert "resource_id" not in r.text and "SELECT" not in r.text
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", failed)


@pytest.mark.anyio
async def test_revoked_assignee_is_visible_to_manager_as_invalid_not_finished(collaborative):
    c = collaborative
    tid = (await assign(c)).json()["task_ids"][0]
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.marker_a_grant["grant_id"])
        )
    d = (await view(c)).json()
    assert d["counts"]["task_invalidated"] == 1
    assert next(i for i in d["items"] if i["kind"] == "task_invalidated")["task_id"] == tid
    assert (await view(c, "marker_a")).status_code == 404


@pytest.mark.anyio
async def test_revoked_scorer_marks_existing_candidate_invalid_without_task(collaborative):
    from test_exam_grading import confirm

    from huipi_cloud.modules.marking_tasks.permissions import TEACHER_ROLE
    from huipi_cloud.modules.resource_security.models import SecurityScope

    c = collaborative
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=TEACHER_ROLE,
            scope_id=scope,
        )
    assert (await confirm(c)).status_code == 200
    response = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
        json={"request_id": str(uuid4()), "expected_answer_revision": 1},
    )
    assert response.status_code == 202
    assert await c.grade_worker.once()
    before = await view(c, "admin")
    assert before.status_code == 200, before.text
    assert before.json()["counts"].get("scoring_invalidated", 0) == 0
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.mark_grant["grant_id"])
        )
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.manager_grant["grant_id"])
        )
    after = await view(c, "admin")
    assert after.status_code == 200, after.text
    assert after.json()["counts"]["scoring_invalidated"] == 1


@pytest.mark.anyio
async def test_scanner_can_find_unclaimed_pages_without_roster_or_grading_access(marking):
    from huipi_cloud.modules.resource_security.models import SecurityScope
    from huipi_cloud.modules.scans.permissions import SCAN_OPERATOR_ROLE

    c = marking
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["b"].id,
            role_version_id=SCAN_OPERATOR_ROLE,
            scope_id=scope,
        )
    response = await view(c, "b")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["counts"].get("unclaimed_page", 0) == 2
    assert result["capabilities"]["pages"] and not result["capabilities"]["scripts"]
    assert not result["capabilities"]["grading"] and not result["capabilities"]["parsing"]
    assert "student_name" not in response.text and "question_id" not in response.text
    page = next(item for item in result["items"] if item["kind"] == "unclaimed_page")
    preview = await c.client.get(
        f"/api/v1/scan-pages/{page['page_id']}/preview", headers=c.headers("b")
    )
    assert preview.status_code == 200 and preview.headers["content-type"].startswith("image/jpeg")
    assert (
        await c.client.get(
            f"/api/v1/exam-sessions/{c.session['id']}/answer-scripts", headers=c.headers("b")
        )
    ).status_code == 404
