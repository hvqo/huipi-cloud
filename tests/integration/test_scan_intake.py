"""Signed JWT + real independent PostgreSQL/S3 PDF intake. No model inference."""

import asyncio
import hashlib
from datetime import date
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import event, func, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from test_exam_rosters import create, entries, freeze, ready
from test_exam_rosters import roster as roster
from test_exam_sessions import exam as exam
from test_paper_versions import papers as papers
from test_teacher_authorization import auth_api as auth_api

from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.main import app
from huipi_cloud.modules.exam_rosters.models import ExamParticipation
from huipi_cloud.modules.organization.models import ClassEnrollment, Student
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityScope
from huipi_cloud.modules.scans import service as scans
from huipi_cloud.modules.scans.models import ScanBatch, ScanFile, ScanOperation, ScanPage
from huipi_cloud.modules.scans.permissions import SCAN_OPERATOR_ROLE
from huipi_cloud.modules.submissions.models import ParsingTask, Submission


def pdf_bytes(count=4, label="Synthetic mixed-class exam"):
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for number in range(count):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        name = "Same Name" if number < 2 else f"Student {number + 1}"
        stream.set_data(
            (
                f"BT /F1 20 Tf 55 700 Td ({label} - sheet {number + 1}) Tj "
                f"0 -40 Td (Name: {name}) Tj "
                "0 -40 Td (Question 1: Calculate 2 + 2.) Tj "
                "0 -40 Td (Student response: 2 + 2 = 4.) Tj ET"
            ).encode("ascii")
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def mixed_scan_pdf():
    """Image-only PDF simulating four unmarked mixed-class answer sheets."""
    from PIL import Image, ImageDraw

    images = []
    for index in range(4):
        image = Image.new("RGB", (612, 792), "white")
        draw = ImageDraw.Draw(image)
        name = "Same Name" if index < 2 else f"Student {index + 1}"
        draw.text((45, 55), f"Synthetic mixed-class exam - page {index + 1}", fill="black")
        draw.text((45, 95), f"Name: {name} / Class: {index % 2 + 1}", fill="black")
        draw.text((45, 140), "Question 1: Calculate 2 + 2.", fill="black")
        draw.text((45, 180), "Student response: 2 + 2 = 4.", fill="black")
        images.append(image)
    output = BytesIO()
    try:
        images[0].save(output, format="PDF", save_all=True, append_images=images[1:])
        return output.getvalue()
    finally:
        for image in images:
            image.close()


@pytest.fixture
async def scan_context(roster):
    c = roster
    async with c.factory() as db, db.begin():
        await db.execute(
            update(Student).where(Student.id == c.students[2].id).values(name="合成乙")
        )
        student = Student(name="合成丙", school_student_code=uuid4().hex)
        db.add(student)
        await db.flush()
        db.add(
            ClassEnrollment(
                student_id=student.id,
                administrative_class_id=c.classes[1].id,
                valid_from=date(2026, 9, 1),
            )
        )
        c.students.append(student)
    r = await create(c, entries(c, 4))
    await ready(c)
    c.frozen_roster = await freeze(c, r)
    async with c.factory() as db:
        await scans.enable_operator(
            db, c.users["admin"].id, c.users["admin"].id, uuid4(), "synthetic scan deployment"
        )
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        c.scan_grant = await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=SCAN_OPERATOR_ROLE,
            scope_id=scope,
        )
    c.client.headers.update(c.headers("a"))
    response = await c.client.post(
        f"/api/v1/exam-sessions/{c.session['id']}/scan-batches",
        json=dict(request_id=str(uuid4()), title="合成混班扫描"),
    )
    assert response.status_code == 201, response.text
    c.batch = response.json()
    try:
        yield c
    finally:
        async with c.factory() as db, db.begin():
            await db.execute(text("TRUNCATE scan_operations,scan_batches CASCADE"))


async def upload(
    c, data=None, request_id=None, filename="mixed.pdf", content_type="application/pdf"
):
    return await c.client.post(
        f"/api/v1/scan-batches/{c.batch['id']}/files",
        data={"request_id": str(request_id or uuid4())},
        files={"file": (filename, pdf_bytes() if data is None else data, content_type)},
    )


@pytest.mark.anyio
async def test_real_upload_pages_download_hash_and_legacy_worker_isolation(scan_context):
    c = scan_context
    payload = mixed_scan_pdf()
    response = await upload(c, payload)
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["sha256"] == hashlib.sha256(payload).hexdigest()
    assert (
        value["size_bytes"] == len(payload)
        and value["page_count"] == 4
        and value["status"] == "stored"
    )
    assert not {"bucket", "object_key", "attempt_token"} & value.keys()
    original = await c.client.get(f"/api/v1/scan-files/{value['id']}/original")
    assert original.status_code == 200 and original.content == payload
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.exceptions import ClientError

    async with c.factory() as db:
        private_key = await db.scalar(select(ScanFile.object_key))
    anonymous = boto3.client(
        "s3",
        endpoint_url=c.storage.storage._client.meta.endpoint_url,
        region_name="us-east-1",
        config=Config(signature_version=UNSIGNED, retries={"max_attempts": 0}),
    )
    try:
        with pytest.raises(ClientError) as denied:
            await asyncio.to_thread(anonymous.head_object, Bucket=c.storage.bucket, Key=private_key)
        assert denied.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
    finally:
        anonymous.close()
    assert original.headers["content-type"] == "application/pdf"
    assert "attachment" in original.headers["content-disposition"]
    pages = (await c.client.get(f"/api/v1/scan-files/{value['id']}/pages")).json()["items"]
    assert len(pages) == 4 and [p["page_index"] for p in pages] == list(range(4))
    for p in pages:
        image = await c.client.get(f"/api/v1/scan-pages/{p['id']}/preview")
        assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
        assert image.content.startswith(b"\xff\xd8")
        assert hashlib.sha256(image.content).hexdigest() == p["sha256"]
        assert p["identity_status"] == "unconfirmed" and max(p["width"], p["height"]) <= 1600
        assert "private" in image.headers["cache-control"]
        assert "object_key" not in p
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ExamParticipation)) == 4
        assert await db.scalar(select(func.count()).select_from(Submission)) == 0
        assert await db.scalar(select(func.count()).select_from(ParsingTask)) == 0
        assert await db.scalar(select(ScanBatch.roster_revision_id)) == UUID(c.frozen_roster["id"])
        with pytest.raises(DBAPIError):
            await db.execute(update(ScanPage).values(identity_status="confirmed"))
        await db.rollback()
        with pytest.raises(DBAPIError):
            await db.execute(update(ScanFile).values(sha256="0" * 64))
        await db.rollback()


@pytest.mark.anyio
async def test_replay_duplicate_and_changed_body(scan_context):
    c = scan_context
    rid = uuid4()
    first = await upload(c, request_id=rid)
    replay = await upload(c, request_id=rid)
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json() == replay.json()
    duplicate = await upload(c)
    assert duplicate.status_code == 409 and duplicate.json()["detail"] == "scan_duplicate_file"
    changed = await upload(c, pdf_bytes(label="Different source"), request_id=rid)
    assert changed.status_code == 409 and changed.json()["detail"] == "scan_request_conflict"
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ScanFile)) == 1
        assert await db.scalar(select(func.count()).select_from(ScanPage)) == 4


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["broken", "too_many", "wrong_type", "empty", "oversize"])
async def test_invalid_and_limited_files_have_no_success_pages(scan_context, monkeypatch, kind):
    c = scan_context
    data = b"%PDF-1.7\nnot a PDF"
    kwargs = {}
    expected = 400
    if kind == "too_many":
        data = pdf_bytes(51)
        expected = 413
    if kind == "wrong_type":
        data = pdf_bytes()
        kwargs = {"content_type": "image/png"}
        expected = 415
    if kind == "empty":
        data = b""
    if kind == "oversize":
        monkeypatch.setattr(scans.settings, "scan_max_file_bytes", 1024)
        data = pdf_bytes()
        expected = 413
    response = await upload(c, data, **kwargs)
    assert response.status_code == expected, response.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ScanPage)) == 0
        assert (
            await db.scalar(
                select(func.count()).select_from(ScanFile).where(ScanFile.status == "stored")
            )
            == 0
        )
        if kind in {"broken", "too_many"}:
            failure = await db.scalar(select(ScanFile))
            assert failure.status == "failed" and failure.failure_code.startswith("scan_")
    assert not c.storage.uploaded_keys


@pytest.mark.anyio
async def test_storage_failure_retries_stable_file_id_with_new_attempt(scan_context, monkeypatch):
    c = scan_context
    original = c.storage.upload_path
    rid = uuid4()

    async def fail(*args):
        await original(*args)
        raise StorageUnavailableError()

    monkeypatch.setattr(c.storage, "upload_path", fail)
    result = await upload(c, request_id=rid)
    assert result.status_code == 503
    async with c.factory() as db:
        value = await db.scalar(select(ScanFile))
        old_id, old_key = value.id, value.object_key
        assert value.status == "failed"
        assert await db.scalar(select(func.count()).select_from(ScanPage)) == 0
    monkeypatch.setattr(c.storage, "upload_path", original)
    second = await upload(c, request_id=rid)
    assert second.status_code == 201 and second.json()["id"] == str(old_id)
    assert second.json()["attempt_number"] == 2
    async with c.factory() as db:
        assert await db.scalar(select(ScanFile.object_key)) != old_key
    assert old_key in c.storage.uploaded_keys  # retained orphan, never delete possible references


@pytest.mark.anyio
async def test_concurrent_replay_only_one_completion(scan_context, monkeypatch):
    c = scan_context
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.storage.upload_path

    async def held(*args):
        await original(*args)
        entered.set()
        await release.wait()

    monkeypatch.setattr(c.storage, "upload_path", held)
    rid = uuid4()
    first = asyncio.create_task(upload(c, request_id=rid))
    await asyncio.wait_for(entered.wait(), 15)
    try:
        other = await upload(c, request_id=rid)
        assert other.status_code == 409 and other.json()["detail"] == "scan_upload_in_progress"
    finally:
        release.set()
    assert (await first).status_code == 201
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ScanFile)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ScanOperation)
                .where(ScanOperation.action == "upload_stored")
            )
            == 1
        )


@pytest.mark.anyio
async def test_scopes_legacy_admin_jwt_forgery_revocation_and_no_enumeration(scan_context):
    c = scan_context
    response = await upload(c)
    fid = response.json()["id"]
    pid = (await c.client.get(f"/api/v1/scan-files/{fid}/pages")).json()["items"][0]["id"]
    async with c.factory() as db, db.begin():
        from huipi_cloud.modules.identity.models import User

        await db.execute(update(User).where(User.id == c.users["b"].id).values(is_admin=True))
    for who in ("b", "reader"):
        headers = c.headers(who, role="admin", teacher_id=str(c.users["a"].id))
        assert (await c.client.get("/api/v1/scan-sessions", headers=headers)).json()["items"] == []
        for path in (
            f"/scan-batches/{c.batch['id']}",
            f"/scan-files/{fid}",
            f"/scan-files/{fid}/pages",
            f"/scan-files/{fid}/original",
            f"/scan-pages/{pid}/preview",
            f"/scan-files/{uuid4()}/original",
        ):
            denied = await c.client.get("/api/v1" + path, headers=headers)
            assert denied.status_code == 404
            assert denied.json() == {"detail": "resource_not_found_or_forbidden"}
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.scan_grant["grant_id"])
        )
    assert (await upload(c, request_id=UUID(response.json()["request_id"]))).status_code == 404
    assert (await c.client.get(f"/api/v1/scan-files/{fid}/original")).status_code == 404
    anonymous = await c.client.get(
        f"/api/v1/scan-pages/{pid}/preview", headers={"Authorization": ""}
    )
    assert anonymous.status_code == 401


@pytest.mark.anyio
async def test_audit_failure_rolls_back_page_index(scan_context):
    c = scan_context

    def fail(conn, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO scan_operations") and (
            context.compiled_parameters[0].get("action") == "upload_stored"
        ):
            raise OperationalError("synthetic", {}, Exception("audit unavailable"))

    engine = c.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", fail)
    try:
        result = await upload(c)
        assert result.status_code == 503
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ScanPage)) == 0
        assert (await db.scalar(select(ScanFile))).status == "failed"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ScanOperation)
                .where(ScanOperation.action == "upload_stored")
            )
            == 0
        )


@pytest.mark.anyio
async def test_commit_unknown_keeps_objects_and_replay_recovers(scan_context):
    c = scan_context
    rid = uuid4()

    async def lost_session():
        async with c.factory() as db:
            original = db.commit
            commits = 0

            async def commit():
                nonlocal commits
                commits += 1
                await original()
                if commits == 2:
                    raise OperationalError("synthetic", {}, Exception("lost ACK"))

            db.commit = commit
            yield db

    previous = app.dependency_overrides[get_db_session]
    app.dependency_overrides[get_db_session] = lost_session
    try:
        first = await upload(c, request_id=rid)
        assert first.status_code == 503 and first.json()["detail"] == "scan_commit_outcome_unknown"
    finally:
        app.dependency_overrides[get_db_session] = previous
    replay = await upload(c, request_id=rid)
    assert replay.status_code == 200 and replay.json()["status"] == "stored"
    original = await c.client.get(f"/api/v1/scan-files/{replay.json()['id']}/original")
    assert original.status_code == 200 and original.content == pdf_bytes()


@pytest.mark.anyio
async def test_cross_session_scope_cannot_list_or_upload(scan_context):
    from test_exam_sessions import new_session

    c = scan_context
    other = await new_session(c, offset=4)
    listed = (await c.client.get("/api/v1/scan-sessions")).json()["items"]
    assert [s["id"] for s in listed] == [c.session["id"]]
    for method in ("get", "post"):
        response = await getattr(c.client, method)(
            f"/api/v1/exam-sessions/{other['id']}/scan-batches",
            **({"json": {"request_id": str(uuid4()), "title": "越权"}} if method == "post" else {}),
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "resource_not_found_or_forbidden"}
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ScanBatch)) == 1


@pytest.mark.anyio
async def test_interrupted_real_upload_is_failed_and_retry_recovers(scan_context, monkeypatch):
    c = scan_context
    entered, release = asyncio.Event(), asyncio.Event()
    real = c.storage.upload_path

    async def held(*args):
        await real(*args)
        entered.set()
        await release.wait()

    monkeypatch.setattr(c.storage, "upload_path", held)
    rid = uuid4()
    task = asyncio.create_task(upload(c, request_id=rid))
    await asyncio.wait_for(entered.wait(), 15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with c.factory() as db:
        f = await db.scalar(select(ScanFile))
        assert f.status == "failed" and f.failure_code == "scan_upload_interrupted"
        assert await db.scalar(select(func.count()).select_from(ScanPage)) == 0
        fid = str(f.id)
    monkeypatch.setattr(c.storage, "upload_path", real)
    response = await upload(c, request_id=rid)
    assert response.status_code == 201 and response.json()["id"] == fid
    assert response.json()["attempt_number"] == 2


@pytest.mark.anyio
async def test_mutated_s3_source_rejected_before_response_bytes(scan_context):
    c = scan_context
    value = (await upload(c)).json()
    async with c.factory() as db:
        f = await db.scalar(select(ScanFile))
        key = f.object_key
    await c.storage.upload_fileobj(BytesIO(b"not the stored PDF"), key, "application/pdf")
    response = await c.client.get(f"/api/v1/scan-files/{value['id']}/original")
    assert response.status_code == 503
    assert response.json() == {"detail": "scan_source_integrity_failed"}
    assert b"not the stored" not in response.content
    async with c.factory() as db:
        assert (await db.scalar(select(ScanFile))).status == "stored"


@pytest.mark.anyio
async def test_encrypted_pdf_cannot_become_stored(scan_context):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("synthetic password")
    output = BytesIO()
    writer.write(output)
    response = await upload(scan_context, output.getvalue())
    assert response.status_code == 400
    async with scan_context.factory() as db:
        assert (await db.scalar(select(ScanFile))).status == "failed"
        assert await db.scalar(select(func.count()).select_from(ScanPage)) == 0
    assert not scan_context.storage.uploaded_keys


@pytest.mark.anyio
async def test_scan_migration_roundtrip_preserves_existing_business_graph(roster, postgres_engine):
    import json

    from test_organization_migration import alembic

    from huipi_cloud.infrastructure.database.base import Base

    url = postgres_engine.url
    assert url.database.endswith("_test")
    # This fixture contains legacy homework, users, organization, locked paper, exam and roster.
    tables = [
        t
        for t in Base.metadata.sorted_tables
        if not t.name.startswith(
            (
                "scan_",
                "security_",
                "permission_",
                "exam_parsing_",
                "exam_submission_links",
                "answer_script",
                "current_page_allocations",
            )
        )
    ]

    async def snapshot():
        async with roster.factory() as db:
            result = {}
            for table in tables:
                values = list(
                    (await db.execute(select(table).order_by(*table.primary_key))).mappings()
                )
                result[table.name] = hashlib.sha256(
                    json.dumps([dict(v) for v in values], sort_keys=True, default=str).encode()
                ).hexdigest()
            return result

    before = await snapshot()
    try:
        await alembic(url, "downgrade", "b12c9d20e411")
        assert await snapshot() == before
    finally:
        await alembic(url, "upgrade", "head")
    await alembic(url, "check")
    assert await snapshot() == before
    async with roster.factory() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "b14b41a2e903"


@pytest.mark.anyio
async def test_scan_facts_refuse_destructive_downgrade(scan_context, postgres_engine):
    from test_organization_migration import alembic

    assert postgres_engine.url.database.endswith("_test")
    await upload(scan_context)
    with pytest.raises(AssertionError):
        await alembic(postgres_engine.url, "downgrade", "b12c9d20e411")
    async with scan_context.factory() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "b14b41a2e903"
        assert await db.scalar(select(func.count()).select_from(ScanPage)) == 4


@pytest.mark.anyio
async def test_revoked_during_storage_io_cannot_commit_pages(scan_context, monkeypatch):
    c = scan_context
    entered, release = asyncio.Event(), asyncio.Event()
    real = c.storage.upload_path

    async def held(*args):
        await real(*args)
        entered.set()
        await release.wait()

    monkeypatch.setattr(c.storage, "upload_path", held)
    task = asyncio.create_task(upload(c))
    await asyncio.wait_for(entered.wait(), 15)
    try:
        async with c.factory() as db:
            await security.revoke_grant(
                db, c.principals["admin"], uuid4(), grant_id=UUID(c.scan_grant["grant_id"])
            )
    finally:
        release.set()
    result = await task
    assert result.status_code == 404
    assert result.json() == {"detail": "resource_not_found_or_forbidden"}
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(ScanPage)) == 0
        assert (await db.scalar(select(ScanFile))).status == "failed"
        assert await db.scalar(select(func.count()).select_from(ExamParticipation)) == 4
