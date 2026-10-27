"""Signed HTTP + isolated PostgreSQL/private S3 + real OpenCV and PDF rendering."""

# ruff: noqa: F401, F811
import asyncio
from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from PIL import ImageDraw
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import DBAPIError
from test_exam_results import allow
from test_exam_rosters import exam, roster
from test_paper_versions import papers as base_papers
from test_scan_intake import auth_api, scan_context, upload

from huipi_cloud.modules.answer_scripts import service as scripts
from huipi_cloud.modules.answer_scripts.permissions import SCRIPT_OPERATOR_ROLE
from huipi_cloud.modules.assignments.models import AnswerKey, Question
from huipi_cloud.modules.exam_grading import service as grading
from huipi_cloud.modules.exam_grading.permissions import MARK_OPERATOR_ROLE
from huipi_cloud.modules.omr.engine import bubble_center
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityScope


@pytest.fixture
async def papers(base_papers):
    """Change synthetic legacy source before the existing fixture snapshots/locks it."""
    c = base_papers
    async with c.factory() as db, db.begin():
        for i, qid in enumerate(c.qids):
            await db.execute(
                update(Question).where(Question.id == qid).values(question_type="single_choice")
            )
            await db.execute(
                update(AnswerKey).where(AnswerKey.question_id == qid).values(answer_content="AB"[i])
            )
    yield c


@pytest.fixture
async def omr_scan(scan_context):
    c = scan_context
    async with c.factory() as db:
        await scripts.enable_operator(
            db, c.users["admin"].id, c.users["admin"].id, uuid4(), "synthetic"
        )
    async with c.factory() as db:
        await grading.enable_operator(
            db, c.users["admin"].id, c.users["admin"].id, uuid4(), "synthetic"
        )
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    for role in [SCRIPT_OPERATOR_ROLE, UUID(MARK_OPERATOR_ROLE)]:
        async with c.factory() as db:
            grant = await security.grant_role(
                db,
                c.principals["admin"],
                uuid4(),
                user_id=c.users["a"].id,
                role_version_id=role,
                scope_id=scope,
            )
            if role == UUID(MARK_OPERATOR_ROLE):
                c.mark_grant = grant
    admin = c.headers("admin")
    pid = c.paper["paper_version_id"]
    view = await c.client.get(f"/api/v1/omr/papers/{pid}", headers=admin)
    assert view.status_code == 200, view.text
    questions = view.json()["questions"]
    assert [q["correct_choice"] for q in questions] == ["A", "B"]
    create = await c.client.post(
        f"/api/v1/omr/papers/{pid}",
        headers=admin,
        json=dict(request_id=str(uuid4()), mapping_ids=[q["mapping_id"] for q in questions]),
    )
    assert create.status_code == 201, create.text
    c.template_id = create.json()["id"]
    pdf = await c.client.get(f"/api/v1/omr/templates/{c.template_id}/print.pdf", headers=admin)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf.content)
    image = document[0].render(scale=150 / 72).to_pil().convert("RGB")
    draw = ImageDraw.Draw(image)
    x, y = bubble_center(0, 0)
    draw.ellipse((x - 16, y - 16, x + 16, y + 16), fill="black")
    out = BytesIO()
    image.save(out, format="PDF", resolution=150)
    document.close()
    uploaded = await upload(c, out.getvalue(), filename="synthetic-omr.pdf")
    assert uploaded.status_code == 201, uploaded.text
    scanned = await c.client.get(f"/api/v1/scan-files/{uploaded.json()['id']}/pages")
    page_id = scanned.json()["items"][0]["id"]
    draft = await c.client.post(
        f"/api/v1/exam-sessions/{c.session['id']}/answer-scripts",
        json=dict(
            request_id=str(uuid4()), page_ids=[page_id], completeness="complete", reason="人工检查"
        ),
    )
    assert draft.status_code == 201, draft.text
    draft = draft.json()
    candidates = (
        await c.client.get(f"/api/v1/exam-sessions/{c.session['id']}/identity-candidates")
    ).json()
    confirmed = await c.client.post(
        f"/api/v1/answer-scripts/{draft['script_id']}/versions",
        json=dict(
            request_id=str(uuid4()),
            expected_revision=draft["revision"],
            action="confirm",
            page_ids=[page_id],
            completeness="complete",
            reason="人工确认学生",
            roster_revision_id=candidates["roster_revision_id"],
            roster_entry_id=candidates["items"][0]["roster_entry_id"],
            session_paper_id=candidates["papers"][0]["session_paper_id"],
        ),
    )
    assert confirmed.status_code == 200, confirmed.text
    c.script_id, c.page_id, c.script_revision = (
        draft["script_id"],
        page_id,
        confirmed.json()["revision"],
    )
    c.results_participation = candidates["items"][0]["participation_id"]
    c.candidates = candidates
    async with c.factory() as db:
        entry = await grading.row(db, scripts.E, UUID(candidates["items"][0]["roster_entry_id"]))
        index = next(
            i for i, cls in enumerate(c.classes) if cls.id == entry["administrative_class_id"]
        )
    await allow(c, index)
    try:
        yield c
    finally:
        async with c.factory() as db, db.begin():
            await db.execute(
                text(
                    "TRUNCATE omr_operations,omr_decisions,omr_recognitions,"
                    "omr_template_questions,omr_templates,exam_result_class_access,"
                    "answer_script_operations,answer_scripts CASCADE"
                )
            )


@pytest.mark.anyio
async def test_real_omr_teacher_confirmation_and_score_export(omr_scan):
    c = omr_scan
    root = f"/api/v1/answer-scripts/{c.script_id}/omr"
    current = await c.client.get(root)
    assert current.status_code == 200, current.text
    request = dict(
        request_id=str(uuid4()),
        template_id=c.template_id,
        scan_page_id=c.page_id,
        expected_revision=c.script_revision,
    )
    scan = await c.client.post(root, json=request)
    assert scan.status_code == 200, scan.text
    assert (await c.client.post(root, json=request)).json() == scan.json()
    rid = scan.json()["id"]
    result = await c.client.get(f"/api/v1/omr/recognitions/{rid}")
    assert result.status_code == 200, result.text
    q = result.json()["questions"]
    assert [(x["status"], x["choice"]) for x in q] == [("single", "A"), ("blank", None)]
    assert (await c.client.get(f"/api/v1/omr/recognitions/{rid}/image")).content.startswith(
        b"\xff\xd8"
    )
    overlay = await c.client.get(f"/api/v1/omr/recognitions/{rid}/image?overlay=true")
    assert overlay.status_code == 200 and overlay.content.startswith(b"\xff\xd8")
    preview_path = f"/api/v1/exam-sessions/{c.session['id']}/results-preview"
    before = (await c.client.get(preview_path)).json()
    student = next(
        s for s in before["students"] if s["participation_id"] == c.results_participation
    )
    assert student["total"] is None and student["subtotal"] is None
    confirmation = dict(
        request_id=str(uuid4()),
        decisions=[
            dict(
                mapping_id=q[0]["mapping_id"],
                expected_revision=0,
                choice="A",
                reason="教师核验清晰填涂",
            )
        ],
    )
    response = await c.client.post(f"/api/v1/omr/recognitions/{rid}/confirm", json=confirmation)
    assert response.status_code == 200, response.text
    assert (
        await c.client.post(f"/api/v1/omr/recognitions/{rid}/confirm", json=confirmation)
    ).json() == response.json()
    partial = (await c.client.get(preview_path)).json()
    student = next(
        s for s in partial["students"] if s["participation_id"] == c.results_participation
    )
    assert (
        student["subtotal"] == "10.00"
        and student["total"] is None
        and student["reviewed_count"] == 1
    )
    second = dict(
        request_id=str(uuid4()),
        decisions=[
            dict(
                mapping_id=q[1]["mapping_id"],
                expected_revision=0,
                choice="blank",
                reason="教师人工核实空白",
            )
        ],
    )
    assert (
        await c.client.post(f"/api/v1/omr/recognitions/{rid}/confirm", json=second)
    ).status_code == 200
    value = (await c.client.get(preview_path)).json()
    student = next(s for s in value["students"] if s["participation_id"] == c.results_participation)
    assert student["total"] == "10.00" and student["reviewed_count"] == 2
    assert [q["evidence_source"] for q in student["questions"]] == ["omr", "omr"]
    exported = await c.client.get(preview_path + ".xlsx")
    assert exported.status_code == 200
    with ZipFile(BytesIO(exported.content)) as archive:
        assert b"10.00" in archive.read("xl/worksheets/sheet1.xml")
    assert (
        await c.client.get(f"/api/v1/omr/recognitions/{rid}", headers=c.headers("b"))
    ).status_code == 404


@pytest.mark.anyio
async def test_concurrent_confirmation_immutable_history_and_corrected_source(omr_scan):
    c = omr_scan
    root = f"/api/v1/answer-scripts/{c.script_id}/omr"
    data = dict(
        request_id=str(uuid4()),
        template_id=c.template_id,
        scan_page_id=c.page_id,
        expected_revision=c.script_revision,
    )
    assert (
        await c.client.post(root, json={**data, "scan_page_id": str(uuid4())})
    ).status_code == 404
    result = await c.client.post(root, json=data)
    assert result.status_code == 200, result.text
    rid = result.json()["id"]
    q = (await c.client.get(f"/api/v1/omr/recognitions/{rid}")).json()["questions"][0]

    async def send(choice):
        return await c.client.post(
            f"/api/v1/omr/recognitions/{rid}/confirm",
            json=dict(
                request_id=str(uuid4()),
                decisions=[
                    dict(
                        mapping_id=q["mapping_id"],
                        expected_revision=0,
                        choice=choice,
                        reason="教师核验",
                    )
                ],
            ),
        )

    first, second = await asyncio.gather(send("A"), send("B"))
    assert sorted([first.status_code, second.status_code]) == [200, 409], (first.text, second.text)
    async with c.factory() as db:
        with pytest.raises(DBAPIError):
            await db.execute(
                text("UPDATE omr_decisions SET points=9 WHERE recognition_id=:id"),
                {"id": UUID(rid)},
            )
        await db.rollback()
        with pytest.raises(DBAPIError):
            await db.execute(
                text("UPDATE omr_template_questions SET ordinal=5 WHERE template_id=:id"),
                {"id": UUID(c.template_id)},
            )
        await db.rollback()
        with pytest.raises(DBAPIError):
            await db.execute(
                text(
                    "INSERT INTO omr_template_questions "
                    "(template_id, ordinal, paper_version_id, mapping_id, printed_label) "
                    "SELECT template_id, 7, paper_version_id, mapping_id, printed_label "
                    "FROM omr_template_questions WHERE template_id=:id LIMIT 1"
                ),
                {"id": UUID(c.template_id)},
            )
        await db.rollback()
    new_version = await c.client.post(
        f"/api/v1/answer-scripts/{c.script_id}/versions",
        json=dict(
            request_id=str(uuid4()),
            expected_revision=c.script_revision,
            action="confirm",
            page_ids=[c.page_id],
            completeness="complete",
            reason="重新核对",
            roster_revision_id=c.candidates["roster_revision_id"],
            roster_entry_id=c.candidates["items"][0]["roster_entry_id"],
            session_paper_id=c.candidates["papers"][0]["session_paper_id"],
        ),
    )
    assert new_version.status_code == 200, new_version.text
    assert (await c.client.get(f"/api/v1/omr/recognitions/{rid}")).status_code == 409
    preview = await c.client.get(f"/api/v1/exam-sessions/{c.session['id']}/results-preview")
    assert preview.status_code == 200, preview.text
    student = next(
        s for s in preview.json()["students"] if s["participation_id"] == c.results_participation
    )
    assert student["total"] is None and student["subtotal"] is None


@pytest.mark.anyio
async def test_revocation_rejects_replay_and_omr_downgrade(omr_scan, postgres_engine):
    from test_organization_migration import alembic

    c = omr_scan
    root = f"/api/v1/answer-scripts/{c.script_id}/omr"
    request = dict(
        request_id=str(uuid4()),
        template_id=c.template_id,
        scan_page_id=c.page_id,
        expected_revision=c.script_revision,
    )
    first = await c.client.post(root, json=request)
    assert first.status_code == 200, first.text
    blocked = await alembic(postgres_engine.url, "downgrade", "b16a82e6a017", success=False)
    assert blocked.returncode != 0 and "禁止破坏性降级" in blocked.stderr
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.mark_grant["grant_id"])
        )
    assert (await c.client.post(root, json=request)).status_code == 404
    assert (await c.client.get(f"/api/v1/omr/recognitions/{first.json()['id']}")).status_code == 404


@pytest.mark.anyio
async def test_corrupt_private_preview_never_creates_recognition(omr_scan):
    c = omr_scan
    async with c.factory() as db:
        rows = await scripts.source_rows(db, UUID(c.session["id"]), [UUID(c.page_id)])
    await c.storage.upload_fileobj(
        BytesIO(b"corrupt synthetic preview"), rows[0]["object_key"], "image/jpeg"
    )
    response = await c.client.post(
        f"/api/v1/answer-scripts/{c.script_id}/omr",
        json=dict(
            request_id=str(uuid4()),
            template_id=c.template_id,
            scan_page_id=c.page_id,
            expected_revision=c.script_revision,
        ),
    )
    assert response.status_code == 503
    async with c.factory() as db:
        assert await db.scalar(text("SELECT count(*) FROM omr_recognitions")) == 0


@pytest.mark.anyio
async def test_unknown_commit_replay_preserves_one_recognition(omr_scan, monkeypatch):
    from huipi_cloud.modules.identity.protocol import AccessError
    from huipi_cloud.modules.omr import schemas, service

    c = omr_scan
    request = schemas.RecognitionCreate(
        request_id=uuid4(),
        template_id=c.template_id,
        scan_page_id=c.page_id,
        expected_revision=c.script_revision,
    )
    async with c.factory() as db:
        commit = db.commit
        calls = 0

        async def lost_ack():
            nonlocal calls
            calls += 1
            await commit()
            if calls == 2:
                raise OSError("synthetic lost commit acknowledgement")

        monkeypatch.setattr(db, "commit", lost_ack)
        with pytest.raises(AccessError) as error:
            await service.recognize(db, c.principals["a"], c.storage, UUID(c.script_id), request)
        assert error.value.code == "omr_commit_unknown"
    async with c.factory() as db:
        replay = await service.recognize(
            db, c.principals["a"], c.storage, UUID(c.script_id), request
        )
    assert replay["id"]
    async with c.factory() as db:
        assert await db.scalar(text("SELECT count(*) FROM omr_recognitions")) == 1
        assert (
            await db.scalar(text("SELECT count(*) FROM omr_operations WHERE action='recognize'"))
            == 1
        )


@pytest.mark.anyio
async def test_audit_failure_rolls_back_recognition(omr_scan, postgres_engine):
    c = omr_scan

    def interrupt(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO omr_operations"):
            raise OSError("synthetic audit failure")

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", interrupt)
    try:
        response = await c.client.post(
            f"/api/v1/answer-scripts/{c.script_id}/omr",
            json=dict(
                request_id=str(uuid4()),
                template_id=c.template_id,
                scan_page_id=c.page_id,
                expected_revision=c.script_revision,
            ),
        )
        assert response.status_code == 503
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", interrupt)
    async with c.factory() as db:
        assert await db.scalar(text("SELECT count(*) FROM omr_recognitions")) == 0
