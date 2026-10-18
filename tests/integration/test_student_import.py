"""B-1.1b: real PostgreSQL + signed JWT integration for the two-phase roster import.

Every scenario runs against the dedicated migrated test database from
``TEST_DATABASE_URL`` (fixture-runs alembic) with the real resource-security
bootstrap/grant/revoke services and the real audit/idempotency layer. All student
data is synthetic.
"""

import asyncio
import hashlib
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from test_teacher_authorization import auth_api as auth_api

from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.organization import import_service
from huipi_cloud.modules.organization.models import (
    AcademicYear,
    AdministrativeClass,
    ClassEnrollment,
    GradeCohort,
    OrganizationOperation,
    Student,
    StudentImportBatch,
    StudentImportRow,
)
from huipi_cloud.modules.organization.schemas import (
    StudentImportBatchPage,
    StudentImportConfirmResult,
    StudentImportPreviewResult,
)
from huipi_cloud.modules.resource_security import service as security_service
from huipi_cloud.modules.resource_security.models import SecurityAudit, SecurityGrant

ACADEMIC_YEAR_START = date(2025, 9, 1)
ACADEMIC_YEAR_END = date(2026, 7, 31)
STUDENT_HEADER = "student_code,name"
ENROLLMENT_HEADER = "student_code,class_code,valid_from"
FULL_HEADER = "student_code,name,class_code,valid_from"
ORG_TRUNCATE = (
    "TRUNCATE TABLE organization_operations, student_import_rows, "
    "student_import_batches, class_enrollments, students, administrative_classes, "
    "subjects, grade_cohorts, academic_terms, academic_years CASCADE"
)


def principal_for(ctx, name="admin") -> Principal:
    user = ctx.users[name]
    return Principal(
        user_id=user.id,
        issuer=user.issuer,
        subject=user.subject,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        is_admin=user.is_admin,
    )


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def csv_document(header: str, rows: list[tuple[str, ...]]) -> bytes:
    lines = [header, *(",".join(cells) for cells in rows)]
    return ("\n".join(lines) + "\n").encode()


def student_csv(*rows: tuple[str, ...]) -> bytes:
    return csv_document(STUDENT_HEADER, list(rows))


def enrollment_csv(*rows: tuple[str, ...]) -> bytes:
    return csv_document(ENROLLMENT_HEADER, list(rows))


def full_csv(*rows: tuple[str, ...]) -> bytes:
    return csv_document(FULL_HEADER, list(rows))


def error_codes(row) -> list[str]:
    return [entry["code"] for entry in row.errors]


async def truncate_organization(factory) -> None:
    async with factory() as session, session.begin():
        await session.execute(text(ORG_TRUNCATE))


@pytest.fixture
async def org(auth_api):
    """Isolated organization state plus a real installation-scoped operator grant."""
    ctx = auth_api
    await truncate_organization(ctx.factory)
    async with ctx.factory() as session:
        initialized = await session.scalar(
            select(SecurityAudit.id).where(SecurityAudit.action == "bootstrap")
        )
    if initialized is None:
        # Fresh dedicated test database: exercise the real deployment bootstrap service.
        async with ctx.factory() as session:
            await security_service.deployment_bootstrap(
                session,
                ctx.users["admin"].id,
                uuid4(),
                "synthetic-approval",
            )
    else:
        # Another run/module already bootstrapped this shared test database; seed the
        # equivalent single installation-scoped operator grant for this test's admin.
        async with ctx.factory() as session, session.begin():
            root = await security_service.installation_scope(session)
            session.add(
                SecurityGrant(
                    user_id=ctx.users["admin"].id,
                    role_version_id=security_service.ORGANIZATION_OPERATOR_ROLE,
                    scope_id=root.id,
                )
            )
            await session.flush()
    try:
        yield ctx
    finally:
        # Release FKs to users before the auth_api fixture removes its test users.
        await truncate_organization(ctx.factory)
        async with ctx.factory() as session, session.begin():
            ids = [user.id for user in ctx.users.values()]
            await session.execute(
                delete(SecurityGrant).where(SecurityGrant.user_id.in_(ids))
            )


async def seed_academic_year(
    factory,
    *,
    status: str = "active",
    start: date = ACADEMIC_YEAR_START,
    end: date = ACADEMIC_YEAR_END,
) -> UUID:
    async with factory() as session, session.begin():
        row = AcademicYear(
            code=f"Y{uuid4().hex[:10]}",
            display_name="合成学年",
            start_date=start,
            end_date=end,
            status=status,
        )
        session.add(row)
        await session.flush()
        return row.id


async def seed_administrative_class(factory, code: str, *, status: str = "active") -> UUID:
    async with factory() as session, session.begin():
        cohort = GradeCohort(
            code=f"C{uuid4().hex[:10]}",
            display_name="合成届",
            intake_year=2025,
        )
        session.add(cohort)
        await session.flush()
        row = AdministrativeClass(
            grade_cohort_id=cohort.id,
            code=code,
            display_name=f"合成班{code}",
            status=status,
        )
        session.add(row)
        await session.flush()
        return row.id


async def seed_student(
    factory, code: str = "S001", name: str = "张三", *, is_active: bool = True
) -> UUID:
    async with factory() as session, session.begin():
        row = Student(school_student_code=code, name=name, is_active=is_active)
        session.add(row)
        await session.flush()
        return row.id


async def seed_enrollment(
    factory,
    student_id: UUID,
    class_id: UUID,
    *,
    valid_from: date = ACADEMIC_YEAR_START,
    valid_to: date | None = None,
) -> UUID:
    async with factory() as session, session.begin():
        row = ClassEnrollment(
            student_id=student_id,
            administrative_class_id=class_id,
            valid_from=valid_from,
            valid_to=valid_to,
            source="manual",
        )
        session.add(row)
        await session.flush()
        return row.id


async def preview(
    ctx,
    *,
    mode: str,
    academic_year_id: UUID,
    content: bytes,
    request_id: UUID | None = None,
    filename: str = "synthetic-roster.csv",
    who: str = "admin",
) -> dict:
    async with ctx.factory() as session:
        return await import_service.preview_student_import(
            session,
            principal_for(ctx, who),
            request_id or uuid4(),
            mode=mode,
            academic_year_id=academic_year_id,
            filename=filename,
            content=content,
        )


async def confirm(
    ctx,
    batch_id,
    *,
    expected_input_sha256: str | None = None,
    request_id: UUID | None = None,
    who: str = "admin",
) -> dict:
    async with ctx.factory() as session:
        return await import_service.confirm_student_import(
            session,
            principal_for(ctx, who),
            request_id or uuid4(),
            batch_id=UUID(str(batch_id)),
            expected_input_sha256=expected_input_sha256,
        )


async def load_rows(ctx, batch_id) -> list[StudentImportRow]:
    async with ctx.factory() as session:
        return list(
            await session.scalars(
                select(StudentImportRow)
                .where(StudentImportRow.batch_id == UUID(str(batch_id)))
                .order_by(StudentImportRow.row_number)
            )
        )


async def table_counts(ctx) -> dict[str, int]:
    async with ctx.factory() as session:
        return {
            "students": await session.scalar(select(func.count()).select_from(Student)),
            "enrollments": await session.scalar(
                select(func.count()).select_from(ClassEnrollment)
            ),
            "batches": await session.scalar(
                select(func.count()).select_from(StudentImportBatch)
            ),
            "rows": await session.scalar(select(func.count()).select_from(StudentImportRow)),
            "operations": await session.scalar(
                select(func.count()).select_from(OrganizationOperation)
            ),
        }


async def race_confirm(
    ctx, attempts: list[tuple[object, str]]
) -> list[dict | AccessError]:
    """Confirm several frozen batches concurrently from the same starting barrier."""
    barrier = asyncio.Barrier(len(attempts))

    async def attempt(batch_id, expected_input_sha256) -> dict | AccessError:
        await barrier.wait()
        async with ctx.factory() as session:
            try:
                return await import_service.confirm_student_import(
                    session,
                    principal_for(ctx),
                    uuid4(),
                    batch_id=UUID(str(batch_id)),
                    expected_input_sha256=expected_input_sha256,
                )
            except AccessError as error:
                return error

    return await asyncio.gather(
        *(attempt(batch_id, expected) for batch_id, expected in attempts)
    )


async def get_batch(ctx, batch_id) -> dict:
    async with ctx.factory() as session:
        return await import_service.get_student_import(
            session, batch_id=UUID(str(batch_id))
        )


async def list_batches(ctx, *, limit: int = 25, offset: int = 0) -> dict:
    async with ctx.factory() as session:
        return await import_service.list_student_imports(
            session, limit=limit, offset=offset
        )


@pytest.mark.anyio
async def test_preview_freezes_batch_without_touching_students_or_enrollments(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_administrative_class(ctx.factory, "G101")
    content = full_csv(
        ("S001", "张三", "G101", "2025-09-01"),
        ("S002", "李四", "G101", "2025-09-01"),
    )
    result = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=content,
        request_id=uuid4(),
    )

    assert result["status"] == "previewed"
    assert result["mode"] == "student_with_enrollment"
    assert result["input_sha256"] == digest(content)
    assert result["file_size_bytes"] == len(content)
    assert result["row_count"] == 2
    assert result["academic_year_id"] == str(year_id)
    assert result["can_confirm"] is True
    assert result["truncated"] is False
    assert result["errors"] == [] and result["warnings"] == []
    assert result["counts"] == {
        "row_total": 2,
        "error_rows": 0,
        "warning_rows": 0,
        "valid_rows": 2,
        "create_students": 2,
        "existing_students": 0,
        "create_enrollments": 2,
        "unchanged_rows": 0,
    }
    StudentImportPreviewResult.model_validate(result)

    counts = await table_counts(ctx)
    assert counts == {
        "students": 0,
        "enrollments": 0,
        "batches": 1,
        "rows": 2,
        "operations": 1,
    }
    async with ctx.factory() as session:
        batch = await session.get(StudentImportBatch, UUID(result["batch_id"]))
        assert batch.mode == "student_with_enrollment"
        assert batch.status == "previewed"
        assert batch.input_sha256 == digest(content)
        assert batch.file_size_bytes == len(content)
        assert batch.row_count == 2
        assert batch.academic_year_id == year_id
        assert batch.requested_by_user_id == ctx.users["admin"].id
        assert batch.preview_counts == result["counts"]
        assert batch.confirmed_at is None and batch.confirm_result is None
    rows = await load_rows(ctx, result["batch_id"])
    assert [row.row_state for row in rows] == ["valid", "valid"]
    assert [row.outcome for row in rows] == [
        "create_student_and_enrollment",
        "create_student_and_enrollment",
    ]
    assert [row.normalized_student_code for row in rows] == ["S001", "S002"]
    assert [row.name for row in rows] == ["张三", "李四"]
    assert [row.class_code for row in rows] == ["G101", "G101"]
    assert [row.valid_from for row in rows] == [date(2025, 9, 1)] * 2

    fetched = await get_batch(ctx, result["batch_id"])
    StudentImportPreviewResult.model_validate(fetched)
    assert fetched["status"] == "previewed" and fetched["can_confirm"] is True
    assert fetched["counts"] == result["counts"]

    page = await list_batches(ctx)
    StudentImportBatchPage.model_validate(page)
    assert page["has_next"] is False and page["offset"] == 0
    assert [record["batch_id"] for record in page["records"]] == [result["batch_id"]]
    assert page["records"][0]["counts"] == result["counts"]
    assert page["records"][0]["confirmed_at"] is None

    with pytest.raises(AccessError) as error:
        await get_batch(ctx, uuid4())
    assert error.value.code == "resource_not_found_or_forbidden"
    assert error.value.status_code == 404


@pytest.mark.anyio
async def test_preview_request_id_is_idempotent_and_conflicting_body_is_rejected(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    first_content = student_csv(("S001", "张三"))
    second_content = student_csv(("S002", "李四"))
    request_id = uuid4()
    first = await preview(
        ctx,
        mode="student_only",
        academic_year_id=year_id,
        content=first_content,
        request_id=request_id,
    )
    replay = await preview(
        ctx,
        mode="student_only",
        academic_year_id=year_id,
        content=first_content,
        request_id=request_id,
    )
    assert replay == first
    assert (await table_counts(ctx))["batches"] == 1

    with pytest.raises(AccessError) as error:
        await preview(
            ctx,
            mode="student_only",
            academic_year_id=year_id,
            content=second_content,
            request_id=request_id,
        )
    assert error.value.code == "organization_request_conflict"
    assert error.value.status_code == 409

    other = await preview(
        ctx,
        mode="student_only",
        academic_year_id=year_id,
        content=second_content,
    )
    assert other["batch_id"] != first["batch_id"]
    assert (await table_counts(ctx))["batches"] == 2


@pytest.mark.anyio
async def test_confirm_creates_students_and_enrollments_with_import_source(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    class_id = await seed_administrative_class(ctx.factory, "G101")
    content = full_csv(
        ("S001", "张三", "G101", "2025-09-01"),
        ("S002", "李四", "G101", "2025-09-01"),
    )
    previewed = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=content,
    )
    confirm_request_id = uuid4()
    result = await confirm(
        ctx,
        previewed["batch_id"],
        expected_input_sha256=digest(content),
        request_id=confirm_request_id,
    )
    StudentImportConfirmResult.model_validate(result)
    assert result["batch_id"] == previewed["batch_id"]
    assert result["status"] == "confirmed"
    assert result["created_students"] == 2
    assert result["created_enrollments"] == 2
    assert result["unchanged_rows"] == 0
    assert result["rejected_rows"] == 0
    assert result["replayed"] is False
    assert datetime.fromisoformat(result["confirmed_at"]).tzinfo is not None

    async with ctx.factory() as session:
        students = {
            row.school_student_code: row
            for row in await session.scalars(select(Student))
        }
        assert {code: row.name for code, row in students.items()} == {
            "S001": "张三",
            "S002": "李四",
        }
        assert all(row.is_active for row in students.values())
        enrollments = list(await session.scalars(select(ClassEnrollment)))
        assert len(enrollments) == 2
        batch_uuid = UUID(previewed["batch_id"])
        for enrollment in enrollments:
            assert enrollment.source == "student_import"
            assert enrollment.source_import_batch_id == batch_uuid
            assert enrollment.administrative_class_id == class_id
            assert enrollment.valid_from == date(2025, 9, 1)
            assert enrollment.valid_to is None
            assert enrollment.student_id in {row.id for row in students.values()}
        batch = await session.get(StudentImportBatch, batch_uuid)
        assert batch.status == "confirmed"
        assert batch.confirmed_by_user_id == ctx.users["admin"].id
        assert batch.confirmed_at is not None
        assert batch.confirm_result == result
        audited = await session.scalar(
            select(func.count())
            .select_from(OrganizationOperation)
            .where(OrganizationOperation.request_id == confirm_request_id)
        )
        assert audited == 1

    fetched = await get_batch(ctx, previewed["batch_id"])
    assert fetched["status"] == "confirmed"
    assert fetched["can_confirm"] is False

    # Same request_id replays the stored confirmation without writing again.
    replay = await confirm(
        ctx,
        previewed["batch_id"],
        expected_input_sha256=digest(content),
        request_id=confirm_request_id,
    )
    assert replay == result
    assert await table_counts(ctx) == {
        "students": 2,
        "enrollments": 2,
        "batches": 1,
        "rows": 2,
        "operations": 2,
    }

    # Same request_id with a different payload is a conflict.
    with pytest.raises(AccessError) as error:
        await confirm(
            ctx,
            previewed["batch_id"],
            expected_input_sha256="0" * 64,
            request_id=confirm_request_id,
        )
    assert error.value.code == "organization_request_conflict"
    assert error.value.status_code == 409

    # A distinct request_id cannot confirm an already confirmed batch.
    with pytest.raises(AccessError) as error:
        await confirm(ctx, previewed["batch_id"], expected_input_sha256=digest(content))
    assert error.value.code == "student_import_already_confirmed"
    assert error.value.status_code == 409

    # Expected-input mismatch is rejected before any write.
    fresh = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=full_csv(("S003", "王五", "G101", "2025-09-01")),
    )
    with pytest.raises(AccessError) as error:
        await confirm(ctx, fresh["batch_id"], expected_input_sha256="0" * 64)
    assert error.value.code == "student_import_conflict"
    assert error.value.status_code == 409


@pytest.mark.anyio
async def test_confirm_supports_student_only_and_enrollment_only_modes(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    class_id = await seed_administrative_class(ctx.factory, "G101")
    await seed_student(ctx.factory, "S001", "张三")

    only = await preview(
        ctx,
        mode="student_only",
        academic_year_id=year_id,
        content=student_csv(("S001", "张三"), ("S002", "李四")),
    )
    rows = await load_rows(ctx, only["batch_id"])
    assert [row.outcome for row in rows] == ["unchanged", "create_student"]
    assert only["counts"]["existing_students"] == 1
    assert only["counts"]["create_students"] == 1
    assert only["counts"]["create_enrollments"] == 0
    only_confirm = await confirm(
        ctx,
        only["batch_id"],
        expected_input_sha256=only["input_sha256"],
    )
    assert only_confirm["created_students"] == 1
    assert only_confirm["created_enrollments"] == 0
    assert only_confirm["unchanged_rows"] == 1
    assert (await table_counts(ctx))["enrollments"] == 0

    enrollment = await preview(
        ctx,
        mode="enrollment_only",
        academic_year_id=year_id,
        content=enrollment_csv(("S001", "G101", "2025-09-01")),
    )
    rows = await load_rows(ctx, enrollment["batch_id"])
    assert [row.outcome for row in rows] == ["enrollment_only_create"]
    assert enrollment["counts"]["create_students"] == 0
    assert enrollment["counts"]["create_enrollments"] == 1
    enrollment_confirm = await confirm(
        ctx,
        enrollment["batch_id"],
        expected_input_sha256=enrollment["input_sha256"],
    )
    assert enrollment_confirm["created_students"] == 0
    assert enrollment_confirm["created_enrollments"] == 1
    async with ctx.factory() as session:
        stored = await session.scalar(select(ClassEnrollment))
        assert stored.source == "student_import"
        assert stored.source_import_batch_id == UUID(enrollment["batch_id"])
        assert stored.administrative_class_id == class_id
        assert stored.valid_from == date(2025, 9, 1)

    again = await preview(
        ctx,
        mode="enrollment_only",
        academic_year_id=year_id,
        content=enrollment_csv(("S001", "G101", "2025-09-01")),
    )
    rows = await load_rows(ctx, again["batch_id"])
    assert [row.outcome for row in rows] == ["unchanged"]
    again_confirm = await confirm(
        ctx,
        again["batch_id"],
        expected_input_sha256=again["input_sha256"],
    )
    assert again_confirm["created_students"] == 0
    assert again_confirm["created_enrollments"] == 0
    assert again_confirm["unchanged_rows"] == 1
    assert (await table_counts(ctx))["enrollments"] == 1


@pytest.mark.anyio
async def test_confirm_rejects_preview_batch_with_blocking_errors(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    previewed = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=full_csv(("S001", "张三", "MISSING", "2025-09-01")),
    )
    assert previewed["can_confirm"] is False
    assert previewed["counts"]["error_rows"] == 1
    rows = await load_rows(ctx, previewed["batch_id"])
    assert error_codes(rows[0]) == ["class_code_not_found"]
    assert rows[0].outcome is None

    with pytest.raises(AccessError) as error:
        await confirm(ctx, previewed["batch_id"])
    assert error.value.code == "student_import_has_blocking_errors"
    assert error.value.status_code == 409

    counts = await table_counts(ctx)
    assert counts["students"] == 0 and counts["enrollments"] == 0
    async with ctx.factory() as session:
        batch = await session.get(StudentImportBatch, UUID(previewed["batch_id"]))
        assert batch.status == "previewed"


@pytest.mark.anyio
async def test_confirm_is_stale_when_planned_student_appears_after_preview(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    previewed = await preview(
        ctx,
        mode="student_only",
        academic_year_id=year_id,
        content=student_csv(("S001", "张三")),
    )
    rows = await load_rows(ctx, previewed["batch_id"])
    assert rows[0].outcome == "create_student"

    await seed_student(ctx.factory, "S001", "张三")
    with pytest.raises(AccessError) as error:
        await confirm(ctx, previewed["batch_id"], expected_input_sha256=digest(
            student_csv(("S001", "张三"))
        ))
    assert error.value.code == "student_import_preview_stale"
    assert error.value.status_code == 409

    counts = await table_counts(ctx)
    assert counts["students"] == 1  # only the concurrently seeded row
    assert counts["enrollments"] == 0
    assert counts["operations"] == 1  # the failed confirm left no audit row
    async with ctx.factory() as session:
        batch = await session.get(StudentImportBatch, UUID(previewed["batch_id"]))
        assert batch.status == "previewed"


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["deactivate", "rename"])
async def test_confirm_is_stale_when_existing_student_state_changes(org, mutation):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_student(ctx.factory, "S001", "张三")
    content = student_csv(("S001", "张三"))
    previewed = await preview(
        ctx, mode="student_only", academic_year_id=year_id, content=content
    )
    rows = await load_rows(ctx, previewed["batch_id"])
    assert rows[0].outcome == "unchanged"

    async with ctx.factory() as session, session.begin():
        changes = {"is_active": False} if mutation == "deactivate" else {"name": "李四"}
        await session.execute(
            update(Student).where(Student.school_student_code == "S001").values(**changes)
        )
    with pytest.raises(AccessError) as error:
        await confirm(
            ctx,
            previewed["batch_id"],
            expected_input_sha256=digest(content),
        )
    assert error.value.code == "student_import_preview_stale"
    assert error.value.status_code == 409
    counts = await table_counts(ctx)
    assert counts["students"] == 1 and counts["enrollments"] == 0


@pytest.mark.anyio
async def test_confirm_is_stale_when_class_is_deactivated_after_preview(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_administrative_class(ctx.factory, "G101")
    content = full_csv(("S001", "张三", "G101", "2025-09-01"))
    previewed = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=content,
    )
    async with ctx.factory() as session, session.begin():
        await session.execute(
            update(AdministrativeClass)
            .where(AdministrativeClass.code == "G101")
            .values(status="inactive")
        )
    with pytest.raises(AccessError) as error:
        await confirm(
            ctx,
            previewed["batch_id"],
            expected_input_sha256=digest(content),
        )
    assert error.value.code == "student_import_preview_stale"
    assert error.value.status_code == 409
    counts = await table_counts(ctx)
    assert counts["students"] == 0 and counts["enrollments"] == 0


@pytest.mark.anyio
async def test_confirm_is_stale_when_another_enrollment_is_inserted(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_administrative_class(ctx.factory, "G101")
    other_class_id = await seed_administrative_class(ctx.factory, "G102")
    student_id = await seed_student(ctx.factory, "S001", "张三")
    content = full_csv(("S001", "张三", "G101", "2025-09-01"))
    previewed = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=content,
    )
    rows = await load_rows(ctx, previewed["batch_id"])
    assert rows[0].outcome == "create_enrollment"

    await seed_enrollment(ctx.factory, student_id, other_class_id)
    with pytest.raises(AccessError) as error:
        await confirm(
            ctx,
            previewed["batch_id"],
            expected_input_sha256=digest(content),
        )
    assert error.value.code == "student_import_preview_stale"
    assert error.value.status_code == 409
    counts = await table_counts(ctx)
    assert counts["students"] == 1 and counts["enrollments"] == 1


@pytest.mark.anyio
async def test_preview_reports_class_date_and_student_conflicts(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_administrative_class(ctx.factory, "D101", status="inactive")
    await seed_administrative_class(ctx.factory, "G101")
    await seed_student(ctx.factory, "S100", "张三")
    content = full_csv(
        ("S001", "张三", "M101", "2025-09-01"),
        ("S002", "李四", "D101", "2025-09-01"),
        ("S003", "王五", "G101", "2024-01-01"),
        ("S100", "李四", "G101", "2025-09-01"),
    )
    result = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=content,
    )
    rows = await load_rows(ctx, result["batch_id"])
    assert [error_codes(row) for row in rows] == [
        ["class_code_not_found"],
        ["administrative_class_inactive"],
        ["valid_from_outside_academic_year"],
        ["student_name_mismatch"],
    ]
    assert [row.outcome for row in rows] == [None, None, None, None]
    assert [row.row_state for row in rows] == ["error"] * 4
    assert result["can_confirm"] is False
    assert result["counts"]["error_rows"] == 4
    assert result["counts"]["valid_rows"] == 0
    assert {
        entry["row_number"]: entry["code"] for entry in result["errors"]
    } == {
        2: "class_code_not_found",
        3: "administrative_class_inactive",
        4: "valid_from_outside_academic_year",
        5: "student_name_mismatch",
    }
    # Structured issues never echo the uploaded student payload.
    for entry in result["errors"]:
        assert set(entry) == {"row_number", "field", "code", "message"}
        assert "S001" not in entry["message"]
        assert "张三" not in entry["message"]

    missing_student = await preview(
        ctx,
        mode="enrollment_only",
        academic_year_id=year_id,
        content=enrollment_csv(("S999", "G101", "2025-09-01")),
    )
    rows = await load_rows(ctx, missing_student["batch_id"])
    assert [error_codes(row) for row in rows] == [["student_code_not_found"]]


@pytest.mark.anyio
async def test_preview_classifies_existing_and_conflicting_enrollments(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    class_id = await seed_administrative_class(ctx.factory, "G101")
    await seed_administrative_class(ctx.factory, "G102")
    student_id = await seed_student(ctx.factory, "S001", "张三")
    await seed_enrollment(ctx.factory, student_id, class_id)

    identical = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=full_csv(("S001", "张三", "G101", "2025-09-01")),
    )
    rows = await load_rows(ctx, identical["batch_id"])
    assert [row.outcome for row in rows] == ["unchanged"]
    assert rows[0].row_state == "valid"
    assert identical["counts"] == {
        "row_total": 1,
        "error_rows": 0,
        "warning_rows": 0,
        "valid_rows": 1,
        "create_students": 0,
        "existing_students": 1,
        "create_enrollments": 0,
        "unchanged_rows": 1,
    }
    assert identical["can_confirm"] is True

    overlap = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=full_csv(("S001", "张三", "G101", "2025-10-01")),
    )
    rows = await load_rows(ctx, overlap["batch_id"])
    assert [error_codes(row) for row in rows] == [["enrollment_overlap"]]

    conflict = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=full_csv(("S001", "张三", "G102", "2025-09-01")),
    )
    rows = await load_rows(ctx, conflict["batch_id"])
    assert [error_codes(row) for row in rows] == [
        ["enrollment_conflict_requires_transfer"]
    ]
    assert conflict["counts"]["error_rows"] == 1
    assert conflict["can_confirm"] is False


@pytest.mark.anyio
async def test_future_enrollment_is_visible_to_preview_on_reimport(org):
    """Future-dated enrollments count for overlap; a re-import is unchanged, not a 409."""
    ctx = org
    today = date.today()
    future_start = today + timedelta(days=60)
    future_end = future_start + timedelta(days=300)
    year_id = await seed_academic_year(ctx.factory, start=future_start, end=future_end)
    await seed_administrative_class(ctx.factory, "N101")
    await seed_administrative_class(ctx.factory, "N102")
    content = full_csv(("S001", "张三", "N101", future_start.isoformat()))

    first = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=content,
    )
    first_confirm = await confirm(
        ctx,
        first["batch_id"],
        expected_input_sha256=digest(content),
    )
    assert first_confirm["created_students"] == 1
    assert first_confirm["created_enrollments"] == 1
    async with ctx.factory() as session:
        enrollment = await session.scalar(select(ClassEnrollment))
        assert enrollment.valid_from > today  # genuinely future-dated, not "current"

    # The second preview must classify the identical future range itself, not defer the
    # duplicate to PostgreSQL's exclusion constraint during confirmation.
    second = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=content,
    )
    rows = await load_rows(ctx, second["batch_id"])
    assert [error_codes(row) for row in rows] == [[]]
    assert [row.outcome for row in rows] == ["unchanged"]
    assert second["can_confirm"] is True
    assert second["counts"]["unchanged_rows"] == 1
    assert second["counts"]["create_enrollments"] == 0
    second_confirm = await confirm(
        ctx,
        second["batch_id"],
        expected_input_sha256=digest(content),
    )
    assert second_confirm["created_students"] == 0
    assert second_confirm["created_enrollments"] == 0
    assert second_confirm["unchanged_rows"] == 1
    assert (await table_counts(ctx))["enrollments"] == 1

    # A different future class overlaps the same open-ended future range in preview.
    other_class = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=full_csv(("S001", "张三", "N102", future_start.isoformat())),
    )
    rows = await load_rows(ctx, other_class["batch_id"])
    assert [error_codes(row) for row in rows] == [
        ["enrollment_conflict_requires_transfer"]
    ]
    assert other_class["can_confirm"] is False
    assert other_class["counts"]["error_rows"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["student_only", "student_with_enrollment"])
async def test_cross_batch_case_only_student_code_is_rejected(org, mode):
    """A second roster using a different case must not create a second identity."""
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_administrative_class(ctx.factory, "G101")
    if mode == "student_only":
        first_content = student_csv(("S001", "张三"))
        colliding_content = student_csv(("s001", "张三"))
    else:
        first_content = full_csv(("S001", "张三", "G101", "2025-09-01"))
        colliding_content = full_csv(("s001", "张三", "G101", "2025-09-01"))

    first = await preview(
        ctx, mode=mode, academic_year_id=year_id, content=first_content
    )
    confirmed = await confirm(
        ctx,
        first["batch_id"],
        expected_input_sha256=digest(first_content),
    )
    assert confirmed["created_students"] == 1
    assert (await table_counts(ctx))["students"] == 1

    colliding = await preview(
        ctx, mode=mode, academic_year_id=year_id, content=colliding_content
    )
    rows = await load_rows(ctx, colliding["batch_id"])
    assert [row.row_state for row in rows] == ["error"]
    assert [error_codes(row) for row in rows] == [
        ["student_code_case_ambiguous_existing"]
    ]
    assert rows[0].outcome is None
    assert colliding["can_confirm"] is False
    assert colliding["counts"]["error_rows"] == 1
    assert colliding["counts"]["create_students"] == 0
    with pytest.raises(AccessError) as error:
        await confirm(
            ctx,
            colliding["batch_id"],
            expected_input_sha256=digest(colliding_content),
        )
    assert error.value.code == "student_import_has_blocking_errors"
    assert error.value.status_code == 409
    counts = await table_counts(ctx)
    assert counts["students"] == 1
    # Only the first confirmed import may write an enrollment.
    assert counts["enrollments"] == (1 if mode == "student_with_enrollment" else 0)

    # Exact-case re-import is still recognized as the same student (no false positive).
    exact = await preview(
        ctx, mode=mode, academic_year_id=year_id, content=first_content
    )
    rows = await load_rows(ctx, exact["batch_id"])
    assert [error_codes(row) for row in rows] == [[]]
    assert [row.outcome for row in rows] == ["unchanged"]
    assert exact["can_confirm"] is True


@pytest.mark.anyio
async def test_enrollment_only_rejects_cross_batch_case_only_code(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_administrative_class(ctx.factory, "G101")
    await seed_student(ctx.factory, "S001", "张三")
    content = enrollment_csv(("s001", "G101", "2025-09-01"))
    previewed = await preview(
        ctx, mode="enrollment_only", academic_year_id=year_id, content=content
    )
    rows = await load_rows(ctx, previewed["batch_id"])
    assert [error_codes(row) for row in rows] == [
        ["student_code_case_ambiguous_existing"]
    ]
    assert [row.row_state for row in rows] == ["error"]
    assert previewed["can_confirm"] is False
    assert previewed["counts"]["create_enrollments"] == 0
    counts = await table_counts(ctx)
    assert counts["students"] == 1
    assert counts["enrollments"] == 0


@pytest.mark.anyio
async def test_same_batch_case_ambiguity_keeps_parser_error_code(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    previewed = await preview(
        ctx,
        mode="student_only",
        academic_year_id=year_id,
        content=student_csv(("C001", "甲"), ("c001", "乙")),
    )
    rows = await load_rows(ctx, previewed["batch_id"])
    assert [error_codes(row) for row in rows] == [
        ["student_code_case_ambiguous"],
        ["student_code_case_ambiguous"],
    ]
    assert previewed["can_confirm"] is False


@pytest.mark.anyio
async def test_preview_truncates_issue_lists_but_keeps_every_frozen_row(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_administrative_class(ctx.factory, "G101")
    failing_rows = [
        (f"S{index:05d}", "学生甲", "MISSING", "2025-09-01") for index in range(501)
    ]
    failing = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=full_csv(*failing_rows),
    )
    assert failing["row_count"] == 501
    assert failing["counts"]["error_rows"] == 501
    assert len(failing["errors"]) == import_service.MAX_PREVIEW_ENTRIES
    assert failing["truncated"] is True
    assert failing["can_confirm"] is False
    assert len(await load_rows(ctx, failing["batch_id"])) == 501

    warning_rows = [
        (f"W{index:05d}", "同名同学", "G101", "2025-09-01") for index in range(501)
    ]
    warned = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=full_csv(*warning_rows),
    )
    assert warned["counts"]["warning_rows"] == 501
    assert warned["counts"]["error_rows"] == 0
    assert len(warned["warnings"]) == import_service.MAX_PREVIEW_ENTRIES
    assert warned["truncated"] is True
    assert warned["can_confirm"] is True
    assert len(await load_rows(ctx, warned["batch_id"])) == 501


@pytest.mark.anyio
async def test_duplicate_names_warn_without_blocking_confirm(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_administrative_class(ctx.factory, "G101")
    content = full_csv(
        ("S001", "张三", "G101", "2025-09-01"),
        ("S002", "张三", "G101", "2025-09-01"),
    )
    warned = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=content,
    )
    rows = await load_rows(ctx, warned["batch_id"])
    assert [row.row_state for row in rows] == ["warning", "warning"]
    assert [[entry["code"] for entry in row.warnings] for row in rows] == [
        ["duplicate_name_existing"],
        ["duplicate_name_existing"],
    ]
    assert [row.outcome for row in rows] == [
        "create_student_and_enrollment",
        "create_student_and_enrollment",
    ]
    assert warned["can_confirm"] is True
    assert warned["counts"]["warning_rows"] == 2
    assert warned["counts"]["error_rows"] == 0

    confirmed = await confirm(
        ctx,
        warned["batch_id"],
        expected_input_sha256=digest(content),
    )
    assert confirmed["created_students"] == 2
    assert confirmed["created_enrollments"] == 2

    # A name that already exists under a different code also only warns.
    await seed_student(ctx.factory, "S900", "赵六")
    existing_name = await preview(
        ctx,
        mode="student_only",
        academic_year_id=year_id,
        content=student_csv(("S910", "赵六")),
    )
    rows = await load_rows(ctx, existing_name["batch_id"])
    assert [error_codes(row) for row in rows] == [[]]
    assert [entry["code"] for entry in rows[0].warnings] == [
        "duplicate_name_existing"
    ]
    assert existing_name["can_confirm"] is True


@pytest.mark.anyio
async def test_students_casefold_unique_index_blocks_second_identity(org):
    """The foundation migration installs a functional unique index on lower(code)."""
    ctx = org
    async with ctx.factory() as session:
        indexdef = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = 'students' "
                "AND indexname = 'uq_students_school_code_casefold'"
            )
        )
    assert indexdef is not None
    lowered = indexdef.lower()
    assert "unique" in lowered and "lower" in lowered

    async with ctx.factory() as session, session.begin():
        session.add(Student(school_student_code="CASE-DUP01", name="合成甲"))
    async with ctx.factory() as session:
        session.add(Student(school_student_code="case-dup01", name="合成乙"))
        with pytest.raises(IntegrityError) as error:
            await session.flush()
        await session.rollback()
    assert "uq_students_school_code_casefold" in str(error.value.orig)
    assert (await table_counts(ctx))["students"] == 1


@pytest.mark.anyio
async def test_concurrent_confirm_of_case_only_batches_creates_one_identity(org):
    """Two frozen student_only batches differing only by code case race; one wins."""
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    upper_content = student_csv(("RACE01", "张三"))
    lower_content = student_csv(("race01", "张三"))
    upper = await preview(
        ctx, mode="student_only", academic_year_id=year_id, content=upper_content
    )
    lower = await preview(
        ctx, mode="student_only", academic_year_id=year_id, content=lower_content
    )
    # Both previews legitimately saw an empty students table and planned a creation.
    assert [row.outcome for row in await load_rows(ctx, upper["batch_id"])] == [
        "create_student"
    ]
    assert [row.outcome for row in await load_rows(ctx, lower["batch_id"])] == [
        "create_student"
    ]
    assert (await table_counts(ctx))["students"] == 0

    results = await race_confirm(
        ctx,
        [
            (upper["batch_id"], digest(upper_content)),
            (lower["batch_id"], digest(lower_content)),
        ],
    )
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, AccessError)]
    assert len(successes) == 1, results
    assert len(failures) == 1, results
    assert failures[0].status_code == 409
    assert failures[0].code == "student_import_preview_stale"

    async with ctx.factory() as session:
        # Exactly one identity exists; the loser's INSERT hit the functional unique index
        # and its whole audited transaction rolled back.
        total_students = await session.scalar(select(func.count()).select_from(Student))
        assert total_students == 1
        matching = list(
            await session.scalars(
                select(Student.school_student_code).where(
                    func.lower(Student.school_student_code) == "race01"
                )
            )
        )
        assert len(matching) == 1
        confirmed = await session.scalar(
            select(func.count())
            .select_from(StudentImportBatch)
            .where(
                StudentImportBatch.id.in_(
                    [UUID(upper["batch_id"]), UUID(lower["batch_id"])]
                ),
                StudentImportBatch.status == "confirmed",
            )
        )
        assert confirmed == 1


@pytest.mark.anyio
async def test_concurrent_confirm_with_enrollment_keeps_one_identity_and_enrollment(org):
    """The same race cannot leave two identities or two current homeroom enrollments."""
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    class_a = await seed_administrative_class(ctx.factory, "RACE-A")
    class_b = await seed_administrative_class(ctx.factory, "RACE-B")
    upper_content = full_csv(("RACE02", "张三", "RACE-A", "2025-09-01"))
    lower_content = full_csv(("race02", "张三", "RACE-B", "2025-09-01"))
    upper = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=upper_content,
    )
    lower = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=lower_content,
    )
    assert [row.outcome for row in await load_rows(ctx, upper["batch_id"])] == [
        "create_student_and_enrollment"
    ]
    assert [row.outcome for row in await load_rows(ctx, lower["batch_id"])] == [
        "create_student_and_enrollment"
    ]

    results = await race_confirm(
        ctx,
        [
            (upper["batch_id"], digest(upper_content)),
            (lower["batch_id"], digest(lower_content)),
        ],
    )
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, AccessError)]
    assert len(successes) == 1, results
    assert len(failures) == 1, results
    assert failures[0].status_code == 409
    assert failures[0].code == "student_import_preview_stale"

    counts = await table_counts(ctx)
    assert counts["students"] == 1
    assert counts["enrollments"] == 1
    async with ctx.factory() as session:
        student = await session.scalar(select(Student))
        assert student.school_student_code.casefold() == "race02"
        current = list(
            await session.scalars(
                select(ClassEnrollment).where(
                    ClassEnrollment.student_id == student.id,
                    ClassEnrollment.valid_to.is_(None),
                )
            )
        )
        assert len(current) == 1
        assert current[0].administrative_class_id in {class_a, class_b}
        confirmed = await session.scalar(
            select(func.count())
            .select_from(StudentImportBatch)
            .where(
                StudentImportBatch.id.in_(
                    [UUID(upper["batch_id"]), UUID(lower["batch_id"])]
                ),
                StudentImportBatch.status == "confirmed",
            )
        )
        assert confirmed == 1


@pytest.mark.anyio
async def test_invalid_upload_and_academic_year_errors(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    closed_year_id = await seed_academic_year(ctx.factory, status="closed")

    async def expect_error(content: bytes, code: str, status: int, *, year: UUID = year_id):
        with pytest.raises(AccessError) as error:
            await preview(
                ctx,
                mode="student_only",
                academic_year_id=year,
                content=content,
            )
        assert (error.value.code, error.value.status_code) == (code, status)

    await expect_error(b"", "student_import_file_empty", 422)
    await expect_error(
        "student_code,name\nS001,张三\n".encode("utf-16"),
        "student_import_file_encoding_unsupported",
        415,
    )
    await expect_error(
        b"student_code,name\nS001,\xff\n",
        "student_import_file_encoding_invalid",
        415,
    )
    await expect_error(
        b"x" * (import_service.MAX_FILE_BYTES + 1),
        "student_import_file_too_large",
        413,
    )
    too_many = student_csv(*[(f"S{index:05d}", "学生") for index in range(5001)])
    await expect_error(too_many, "student_import_row_limit_exceeded", 413)
    await expect_error(
        'student_code,name\n"S001,张三\n'.encode(),
        "student_import_csv_malformed",
        422,
    )
    await expect_error(b"student_code\nS001\n", "student_import_columns_invalid", 422)
    with pytest.raises(AccessError) as error:
        await preview(
            ctx,
            mode="student_only",
            academic_year_id=closed_year_id,
            content=student_csv(("S001", "张三")),
        )
    assert error.value.code == "academic_year_closed"
    assert error.value.status_code == 409
    with pytest.raises(AccessError) as error:
        await preview(
            ctx,
            mode="student_only",
            academic_year_id=uuid4(),
            content=student_csv(("S001", "张三")),
        )
    assert error.value.code == "academic_year_not_found"
    assert error.value.status_code == 404
    assert (await table_counts(ctx))["batches"] == 0


@pytest.mark.anyio
async def test_import_permission_revoke_blocks_preview_and_confirm_over_http(org):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_administrative_class(ctx.factory, "G101")
    content = full_csv(("S001", "张三", "G101", "2025-09-01"))
    files = {"file": ("synthetic.csv", content, "text/csv")}
    form = {
        "request_id": str(uuid4()),
        "mode": "student_with_enrollment",
        "academic_year_id": str(year_id),
    }

    unauthorized = await ctx.client.post(
        "/api/v1/organization/student-imports",
        headers=ctx.headers("a"),
        data=form,
        files=files,
    )
    assert unauthorized.status_code in {403, 404}

    created = await ctx.client.post(
        "/api/v1/organization/student-imports",
        headers=ctx.headers("admin"),
        data={**form, "request_id": str(uuid4())},
        files=files,
    )
    assert created.status_code == 201, created.text
    batch = created.json()
    StudentImportPreviewResult.model_validate(batch)
    listing = await ctx.client.get(
        "/api/v1/organization/student-imports", headers=ctx.headers("admin")
    )
    assert listing.status_code == 200
    assert listing.json()["records"][0]["batch_id"] == batch["batch_id"]
    detail = await ctx.client.get(
        f"/api/v1/organization/student-imports/{batch['batch_id']}",
        headers=ctx.headers("admin"),
    )
    assert detail.status_code == 200
    unauthenticated = await ctx.client.get(
        f"/api/v1/organization/student-imports/{batch['batch_id']}"
    )
    assert unauthenticated.status_code == 401

    # Real grant -> preview works for a non-admin.
    async with ctx.factory() as session:
        receipt = await security_service.grant_role(
            session,
            principal_for(ctx, "admin"),
            uuid4(),
            user_id=ctx.users["b"].id,
            role_version_id=security_service.STUDENT_IMPORTER_ROLE,
            scope_id=security_service.INSTALLATION_SCOPE,
        )
    granted = await ctx.client.post(
        "/api/v1/organization/student-imports",
        headers=ctx.headers("b"),
        data={**form, "request_id": str(uuid4())},
        files={"file": ("synthetic-b.csv", content, "text/csv")},
    )
    assert granted.status_code == 201, granted.text
    pending = granted.json()
    assert pending["counts"]["create_students"] == 1

    # Revoking the Grant before confirm blocks confirmation.
    async with ctx.factory() as session:
        await security_service.revoke_grant(
            session,
            principal_for(ctx, "admin"),
            uuid4(),
            grant_id=UUID(receipt["grant_id"]),
        )
    revoked_confirm = await ctx.client.post(
        f"/api/v1/organization/student-imports/{pending['batch_id']}/confirm",
        headers=ctx.headers("b"),
        json={
            "request_id": str(uuid4()),
            "expected_input_sha256": pending["input_sha256"],
        },
    )
    assert revoked_confirm.status_code in {403, 404}
    revoked_preview = await ctx.client.post(
        "/api/v1/organization/student-imports",
        headers=ctx.headers("b"),
        data={**form, "request_id": str(uuid4())},
        files=files,
    )
    assert revoked_preview.status_code in {403, 404}
    async with ctx.factory() as session:
        batch_row = await session.get(
            StudentImportBatch, UUID(pending["batch_id"])
        )
        assert batch_row.status == "previewed"
        assert batch_row.confirmed_at is None

    # The still-authorized operator can confirm the frozen batch.
    confirm_request_id = uuid4()
    confirmed = await ctx.client.post(
        f"/api/v1/organization/student-imports/{pending['batch_id']}/confirm",
        headers=ctx.headers("admin"),
        json={
            "request_id": str(confirm_request_id),
            "expected_input_sha256": pending["input_sha256"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    StudentImportConfirmResult.model_validate(confirmed.json())
    replay = await ctx.client.post(
        f"/api/v1/organization/student-imports/{pending['batch_id']}/confirm",
        headers=ctx.headers("admin"),
        json={
            "request_id": str(confirm_request_id),
            "expected_input_sha256": pending["input_sha256"],
        },
    )
    assert replay.status_code == 200
    assert replay.json() == confirmed.json()
    counts = await table_counts(ctx)
    assert counts["students"] == 1 and counts["enrollments"] == 1


@pytest.mark.anyio
async def test_audit_insert_failure_rolls_back_preview_and_confirm(org, postgres_engine):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_administrative_class(ctx.factory, "G101")
    first = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=full_csv(("S001", "张三", "G101", "2025-09-01")),
    )

    def fail_audit(conn, cursor, statement, params, context, executemany):
        if statement.startswith("INSERT INTO organization_operations"):
            raise OperationalError("audit insert", {}, Exception("synthetic unavailable"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail_audit)
    try:
        with pytest.raises(AccessError) as error:
            await preview(
                ctx,
                mode="student_with_enrollment",
                academic_year_id=year_id,
                content=full_csv(("S002", "李四", "G101", "2025-09-01")),
            )
        assert (error.value.code, error.value.status_code) == (
            "organization_operation_unavailable",
            503,
        )
        counts = await table_counts(ctx)
        assert counts == {
            "students": 0,
            "enrollments": 0,
            "batches": 1,
            "rows": 1,
            "operations": 1,
        }

        with pytest.raises(AccessError) as error:
            await confirm(
                ctx,
                first["batch_id"],
                expected_input_sha256=first["input_sha256"],
            )
        assert (error.value.code, error.value.status_code) == (
            "organization_operation_unavailable",
            503,
        )
        counts = await table_counts(ctx)
        assert counts["students"] == 0
        assert counts["enrollments"] == 0
        async with ctx.factory() as session:
            batch = await session.get(StudentImportBatch, UUID(first["batch_id"]))
            assert batch.status == "previewed"
            assert batch.confirmed_at is None
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail_audit)

    # The same frozen batch is still confirmable once the audit layer recovers.
    recovered = await confirm(
        ctx,
        first["batch_id"],
        expected_input_sha256=first["input_sha256"],
    )
    assert recovered["status"] == "confirmed"
    assert recovered["created_students"] == 1


@pytest.mark.anyio
async def test_lost_commit_ack_recovers_on_replay_without_duplicate_students(org, monkeypatch):
    ctx = org
    year_id = await seed_academic_year(ctx.factory)
    await seed_administrative_class(ctx.factory, "G101")
    content = full_csv(("S001", "张三", "G101", "2025-09-01"))
    previewed = await preview(
        ctx,
        mode="student_with_enrollment",
        academic_year_id=year_id,
        content=content,
    )
    request_id = uuid4()
    commit = ctx.factory.class_.commit

    async def lost_ack(session):
        await commit(session)
        if session.info.pop("lost_ack", False):
            raise OSError("synthetic commit ack lost")

    monkeypatch.setattr(ctx.factory.class_, "commit", lost_ack)
    async with ctx.factory() as session:
        session.info["lost_ack"] = True
        with pytest.raises(AccessError) as error:
            await import_service.confirm_student_import(
                session,
                principal_for(ctx),
                request_id,
                batch_id=UUID(previewed["batch_id"]),
                expected_input_sha256=digest(content),
            )
        assert (error.value.code, error.value.status_code) == (
            "organization_commit_outcome_unknown",
            503,
        )

    recovered = await confirm(
        ctx,
        previewed["batch_id"],
        expected_input_sha256=digest(content),
        request_id=request_id,
    )
    assert recovered["status"] == "confirmed"
    assert recovered["created_students"] == 1
    counts = await table_counts(ctx)
    assert counts["students"] == 1
    assert counts["enrollments"] == 1
    async with ctx.factory() as session:
        batch = await session.get(StudentImportBatch, UUID(previewed["batch_id"]))
        assert batch.status == "confirmed"
        audited = await session.scalar(
            select(func.count())
            .select_from(OrganizationOperation)
            .where(OrganizationOperation.request_id == request_id)
        )
        assert audited == 1
    replay = await confirm(
        ctx,
        previewed["batch_id"],
        expected_input_sha256=digest(content),
        request_id=request_id,
    )
    assert replay == recovered
    assert (await table_counts(ctx))["students"] == 1
