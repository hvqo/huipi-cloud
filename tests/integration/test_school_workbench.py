"""Real signed JWT + isolated PostgreSQL, school UI contracts over original services."""
# ruff: noqa: F401, F811

import asyncio
import json
from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from test_organization import grant_role, org_api

from huipi_cloud.modules.organization import service
from huipi_cloud.modules.organization.csv_import import MODE_COLUMNS
from huipi_cloud.modules.organization.models import (
    ClassEnrollment,
    OrganizationOperation,
    Student,
    Subject,
)
from huipi_cloud.modules.organization.schemas import StudentTransferRequest
from huipi_cloud.modules.resource_security import service as security

BASE = "/api/v1/organization"


async def preview_role(c):
    """Explicit test deployment: sealed read+preview role, never provisioned by HTTP."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    from huipi_cloud.modules.resource_security.models import (
        SecurityRolePermission,
        SecurityRoleVersion,
    )

    rid = uuid4()
    async with c.factory() as db, db.begin():
        db.add(SecurityRoleVersion(id=rid, role_code="school_preview_" + rid.hex, revision=1))
        await db.flush()
        db.add_all(
            [
                SecurityRolePermission(role_version_id=rid, permission_code=code)
                for code in ["organization.read", "student.import.preview"]
            ]
        )
        await db.flush()
        await db.execute(
            update(SecurityRoleVersion)
            .where(SecurityRoleVersion.id == rid)
            .values(sealed_at=datetime.now(UTC))
        )
    return rid


async def post(c, path, body, actor="manager"):
    return await c.client.post(BASE + path, headers=c.headers(actor), json=body)


async def seed(c):
    year = await post(
        c,
        "/academic-years",
        dict(
            request_id=str(uuid4()),
            code=uuid4().hex,
            display_name="合成学年",
            start_date="2026-09-01",
            end_date="2027-07-10",
            status="active",
        ),
    )
    assert year.status_code == 201, year.text
    yid = year.json()["id"]
    cohort = await post(
        c,
        "/grade-cohorts",
        dict(
            request_id=str(uuid4()), code=uuid4().hex, display_name="2026合成届别", intake_year=2026
        ),
    )
    assert cohort.status_code == 201, cohort.text
    classes = []
    for code in ["C1", "C2"]:
        r = await post(
            c,
            "/classes",
            dict(
                request_id=str(uuid4()),
                code=code,
                display_name="合成" + code,
                grade_cohort_id=cohort.json()["id"],
            ),
        )
        assert r.status_code == 201, r.text
        classes.append(r.json()["id"])
    return yid, cohort.json()["id"], classes


async def preview(
    c, yid, content, mode="student_with_enrollment", actor="importer", request_id=None
):
    return await c.client.post(
        BASE + "/student-imports",
        headers=c.headers(actor),
        data=dict(request_id=str(request_id or uuid4()), mode=mode, academic_year_id=yid),
        files={"file": ("synthetic.csv", content, "text/csv")},
    )


async def confirm(c, bid, sha, actor="importer", request_id=None):
    return await post(
        c,
        f"/student-imports/{bid}/confirm",
        dict(request_id=str(request_id or uuid4()), expected_input_sha256=sha),
        actor,
    )


FULL = "student_code,name,class_code,valid_from\n"
DATA = (FULL + "S001,同名合成,C1,2026-09-01\nS002,同名合成,C2,2026-09-01\n").encode()


@pytest.mark.anyio
async def test_current_capabilities_are_independent_current_grants_not_admin_claims(org_api):
    c = org_api
    expected = {
        "manager": (True, True, False, False),
        "importer": (True, False, True, True),
        "teacher": (False, False, False, False),
        "legacy_admin": (False, False, False, False),
    }
    for actor, flags in expected.items():
        r = await c.client.get(
            "/api/v1/me/school-access", headers=c.headers(actor, role="admin", is_admin=True)
        )
        assert r.status_code == 200, r.text
        assert (
            tuple(r.json()[key] for key in ["can_read", "can_manage", "can_preview", "can_confirm"])
            == flags
        )
        assert "no-store" in r.headers["cache-control"]
    await grant_role(c, "teacher", await preview_role(c))
    r = await c.client.get("/api/v1/me/school-access", headers=c.headers("teacher"))
    assert r.json() == dict(can_read=True, can_manage=False, can_preview=True, can_confirm=False)
    assert (await c.client.get("/api/v1/me/school-access")).status_code == 401


@pytest.mark.anyio
async def test_all_five_dictionary_writes_replay_parent_validation_and_no_teacher_access(org_api):
    c = org_api
    yid, cohort, classes = await seed(c)
    body = dict(
        request_id=str(uuid4()),
        academic_year_id=yid,
        term_no=1,
        code="fall",
        start_date="2026-09-01",
        end_date="2027-01-31",
    )
    r = await post(c, "/academic-terms", body)
    assert r.status_code == 201, r.text
    assert (await post(c, "/academic-terms", body)).json() == r.json()
    assert (await post(c, "/academic-terms", dict(body, code="other"))).status_code == 409
    assert (
        await post(c, "/academic-terms", dict(body, request_id=str(uuid4()), end_date="2027-08-01"))
    ).status_code == 422
    subject = dict(request_id=str(uuid4()), code="SYNMATH", display_name="合成数学")
    assert (await post(c, "/subjects", subject)).status_code == 201
    assert (
        await post(c, "/subjects", dict(subject, request_id=str(uuid4())), "teacher")
    ).status_code == 404
    assert (await post(c, "/subjects", dict(subject, role="admin"))).status_code == 422
    for actor in ["teacher", "legacy_admin", "importer"]:
        assert (
            await post(c, "/subjects", dict(subject, request_id=str(uuid4())), actor)
        ).status_code == 404
    assert (
        await c.client.get(
            BASE + "/academic-terms", params={"academic_year_id": yid}, headers=c.headers("manager")
        )
    ).json()["records"][0]["id"] == r.json()["id"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode", list(MODE_COLUMNS))
async def test_templates_match_exact_backend_columns_and_require_preview_permission(org_api, mode):
    c = org_api
    r = await c.client.get(
        BASE + "/student-import-template", params={"mode": mode}, headers=c.headers("importer")
    )
    assert r.status_code == 200
    assert r.content.decode("utf-8-sig") == ",".join(MODE_COLUMNS[mode]) + "\r\n"
    assert "attachment" in r.headers["content-disposition"]
    for actor in ["manager", "teacher", "legacy_admin"]:
        assert (
            await c.client.get(
                BASE + "/student-import-template", params={"mode": mode}, headers=c.headers(actor)
            )
        ).status_code == 404


@pytest.mark.anyio
async def test_csv_preview_rows_confirmation_history_and_distinct_same_name_students(org_api):
    c = org_api
    y, _, classes = await seed(c)
    r = await preview(c, y, DATA)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["status"] == "previewed" and p["can_confirm"]
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Student)) == 0
    rows = await c.client.get(
        BASE + f"/student-imports/{p['batch_id']}/rows", headers=c.headers("importer")
    )
    assert rows.status_code == 200 and len(rows.json()["records"]) == 2
    assert {row["student_code"] for row in rows.json()["records"]} == {"S001", "S002"}
    done = await confirm(c, p["batch_id"], p["input_sha256"])
    assert done.status_code == 200 and done.json()["created_students"] == 2
    async with c.factory() as db:
        values = (await db.scalars(select(Student))).all()
        assert len({v.id for v in values}) == 2 and len({v.name for v in values}) == 1
        operations = (await db.scalars(select(OrganizationOperation))).all()
        audit = json.dumps(
            [dict(result=o.result, payload=o.payload_sha256) for o in operations], default=str
        )
        assert "同名合成" not in audit and "S001" not in audit
    listing = await c.client.get(
        BASE + "/class-summaries",
        headers=c.headers("manager"),
        params=dict(academic_year_id=y, as_of="2026-10-20"),
    )
    assert {r["student_count"] for r in listing.json()["records"]} == {1}
    assert all(r["grade_label"] == "高一" for r in listing.json()["records"])
    assert (await c.client.get(BASE + "/student-imports", headers=c.headers("importer"))).json()[
        "records"
    ][0]["status"] == "confirmed"
    denied = await c.client.get(
        BASE + f"/student-imports/{p['batch_id']}/rows", headers=c.headers("teacher", role="admin")
    )
    assert denied.status_code == 404 and "S001" not in denied.text


@pytest.mark.anyio
async def test_preview_only_never_confirms_and_revoked_import_replay_is_denied(org_api):
    c = org_api
    y, _, _ = await seed(c)
    grant = await grant_role(c, "teacher", await preview_role(c))
    r = await preview(c, y, DATA, actor="teacher")
    p = r.json()
    assert r.status_code == 201, r.text
    assert (await confirm(c, p["batch_id"], p["input_sha256"], actor="teacher")).status_code == 404
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principal("admin"), uuid4(), grant_id=UUID(grant["grant_id"])
        )
    assert (
        await c.client.get(BASE + f"/student-imports/{p['batch_id']}", headers=c.headers("teacher"))
    ).status_code == 404
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Student)) == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content,status",
    [
        (b"\xff\xfe" + b"bad", 415),
        (b"wrong,columns\na,b\n", 422),
        (("student_code,name\n" + "S,合成\n" * 5001).encode(), 413),
        (b"x" * (2 * 1024 * 1024 + 1), 413),
    ],
)
async def test_invalid_files_do_not_create_students(org_api, content, status):
    c = org_api
    y, _, _ = await seed(c)
    r = await preview(c, y, content, mode="student_only")
    assert r.status_code == status, r.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Student)) == 0


@pytest.mark.anyio
async def test_blocking_duplicates_case_dates_class_and_name_conflicts_cannot_confirm(org_api):
    c = org_api
    y, _, _ = await seed(c)
    bad = (
        FULL + "S001,甲,C1,2026-09-01\nS001,甲,C1,2026-09-01\n"
        "s001,甲,C2,2026-09-01\nS003,乙,missing,2026-09-01\nS004,丙,C1,bad-date\n"
    ).encode()
    r = await preview(c, y, bad)
    assert r.status_code == 201, r.text
    p = r.json()
    assert not p["can_confirm"] and p["counts"]["error_rows"] > 0
    assert (await confirm(c, p["batch_id"], p["input_sha256"])).status_code == 409
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Student)) == 0
    valid = (await preview(c, y, DATA)).json()
    assert (await confirm(c, valid["batch_id"], valid["input_sha256"])).status_code == 200
    conflict = (
        await preview(
            c, y, (FULL + "S001,不同姓名,C1,2026-09-01\ns002,同名合成,C2,2026-09-01\n").encode()
        )
    ).json()
    assert not conflict["can_confirm"]
    assert {e["code"] for e in conflict["errors"]} >= {
        "student_name_mismatch",
        "student_code_case_ambiguous_existing",
    }


@pytest.mark.anyio
async def test_same_request_concurrent_confirmation_and_rows_pagination(org_api):
    c = org_api
    y, _, _ = await seed(c)
    data = (FULL + "".join(f"S{i:03},合成{i},C1,2026-09-01\n" for i in range(27))).encode()
    p = (await preview(c, y, data)).json()
    rid = uuid4()
    a, b = await asyncio.gather(
        confirm(c, p["batch_id"], p["input_sha256"], request_id=rid),
        confirm(c, p["batch_id"], p["input_sha256"], request_id=rid),
    )
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    page = await c.client.get(
        BASE + f"/student-imports/{p['batch_id']}/rows",
        params=dict(offset=25, limit=25),
        headers=c.headers("importer"),
    )
    assert len(page.json()["records"]) == 2 and page.json()["has_next"] is False
    assert (
        await c.client.get(
            BASE + f"/student-imports/{p['batch_id']}/rows",
            params=dict(limit=101),
            headers=c.headers("importer"),
        )
    ).status_code == 422
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(Student)) == 27


@pytest.mark.anyio
async def test_business_date_class_counts_and_student_filter_preserve_transfer_history(org_api):
    c = org_api
    y, _, classes = await seed(c)
    p = (await preview(c, y, DATA)).json()
    await confirm(c, p["batch_id"], p["input_sha256"])
    async with c.factory() as db:
        student = await db.scalar(select(Student).where(Student.school_student_code == "S001"))
    async with c.factory() as db:
        await service.transfer_student(
            db,
            c.principal("manager"),
            StudentTransferRequest(
                request_id=uuid4(),
                student_id=student.id,
                to_administrative_class_id=UUID(classes[1]),
                effective_from=date(2026, 11, 1),
            ),
        )
    for day, counts in [
        ("2026-10-20", {classes[0]: 1, classes[1]: 1}),
        ("2026-11-02", {classes[0]: 0, classes[1]: 2}),
    ]:
        r = await c.client.get(
            BASE + "/class-summaries",
            params=dict(academic_year_id=y, as_of=day),
            headers=c.headers("manager"),
        )
        assert {x["id"]: x["student_count"] for x in r.json()["records"]} == counts
        r = await c.client.get(
            BASE + "/students",
            params=dict(class_id=classes[0], as_of=day),
            headers=c.headers("manager"),
        )
        assert len(r.json()["records"]) == counts[classes[0]]


@pytest.mark.anyio
async def test_dictionary_audit_failure_and_actual_commit_ack_loss_recover(
    org_api, postgres_engine, monkeypatch
):
    c = org_api
    body = dict(request_id=str(uuid4()), code="FAULT", display_name="合成学科")

    def fail(conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO organization_operations" in statement:
            raise OperationalError("redacted", None, Exception("synthetic audit failure"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail)
    try:
        assert (await post(c, "/subjects", body)).status_code == 503
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail)
    async with c.factory() as db:
        assert not (await db.scalars(select(Subject).where(Subject.code == "FAULT"))).all()
    commit = c.factory.class_.commit
    original = service.create_subject
    armed = True

    async def lost(db):
        nonlocal armed
        await commit(db)
        if db.info.pop("school_ack", False) and armed:
            armed = False
            raise OSError("synthetic ack lost")

    async def inject(db, *args, **kwargs):
        db.info["school_ack"] = True
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(c.factory.class_, "commit", lost)
    monkeypatch.setattr(service, "create_subject", inject)
    r = await post(c, "/subjects", body)
    assert r.status_code == 503 and r.json()["detail"] == "organization_commit_outcome_unknown"
    r = await post(c, "/subjects", body)
    assert r.status_code == 201, r.text
    async with c.factory() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(Subject).where(Subject.code == "FAULT")
            )
            == 1
        )


@pytest.mark.anyio
async def test_dictionary_json_limit_and_database_failure_fail_closed_without_payload(
    org_api, monkeypatch
):
    c = org_api
    oversized = dict(request_id=str(uuid4()), code="safe", display_name="x" * 9000)
    for content_type in ["application/json", "application/JSON", "multipart/form-data"]:
        r = await c.client.post(
            BASE + "/subjects",
            headers=dict(c.headers("manager"), **{"Content-Type": content_type}),
            content=json.dumps(oversized),
        )
        assert r.status_code == 413 and "x" * 100 not in r.text
    from huipi_cloud.modules.organization import operations

    async def unavailable(*args, **kwargs):
        raise OperationalError("synthetic-db-fault", {}, Exception("private connection"))

    monkeypatch.setattr(operations.security, "require_permission", unavailable)
    for path in ["/api/v1/me/school-access", BASE + "/classes", BASE + "/student-imports"]:
        r = await c.client.get(path, headers=c.headers("manager"))
        assert r.status_code == 503, r.text
        assert "private connection" not in r.text and "synthetic-db-fault" not in r.text


@pytest.mark.anyio
async def test_student_school_filters_use_real_enrollment_date_and_cohort(org_api):
    c = org_api
    year, cohort, classes = await seed(c)
    p = (await preview(c, year, DATA)).json()
    assert (await confirm(c, p["batch_id"], p["input_sha256"])).status_code == 200

    async def listing(**filters):
        return await c.client.get(BASE + "/students", headers=c.headers("manager"), params=filters)

    r = await listing(academic_year_id=year, cohort_id=cohort, as_of="2026-10-20")
    assert {s["school_student_code"] for s in r.json()["records"]} == {"S001", "S002"}
    r = await listing(academic_year_id=year, cohort_id=str(uuid4()), as_of="2026-10-20")
    assert r.status_code == 200 and r.json()["records"] == []
    r = await listing(class_id=classes[0], cohort_id=cohort, as_of="2026-08-01")
    assert r.status_code == 200 and r.json()["records"] == []
    r = await listing(academic_year_id=year, as_of="2028-01-01")
    assert r.status_code == 422
    r = await listing(academic_year_id=str(uuid4()), as_of="2026-10-20")
    assert r.status_code == 404 and "S001" not in r.text
