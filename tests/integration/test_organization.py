"""B-1.1b independent integration tests: real PostgreSQL and signed OIDC tokens.

Covers the academic calendar and homeroom enrollment invariants enforced by PostgreSQL
(trigger/GiST exclusions), history-preserving class transfer under concurrency, the fixed
installation-scoped authorization matrix (real bootstrap + grant/revoke), HTTP contracts,
database-fault translation, legacy assignment regressions and self-cleaning fixtures.

All data is synthetic. The tests target the dedicated ``*_test`` database selected by
``TEST_DATABASE_URL`` and never the development database.
"""

import asyncio
import inspect
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from sqlalchemy import delete, event, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.auth.jwt import TokenVerifier
from huipi_cloud.main import app
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.identity import dependencies as identity_dependencies
from huipi_cloud.modules.identity.models import AssignmentMembership, User
from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.organization import import_service
from huipi_cloud.modules.organization import service as org_service
from huipi_cloud.modules.organization.models import (
    AcademicTerm,
    AcademicYear,
    AdministrativeClass,
    ClassEnrollment,
    GradeCohort,
    OrganizationOperation,
    Student,
    Subject,
)
from huipi_cloud.modules.organization.schemas import (
    AcademicTermCreate,
    AcademicYearCreate,
    StudentTransferRequest,
    SubjectCreate,
)
from huipi_cloud.modules.resource_security import service as security_service
from huipi_cloud.modules.resource_security.models import SecurityAudit, SecurityGrant
from huipi_cloud.modules.submissions.models import Submission

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TODAY = date.today()
YEAR_START = date(2026, 9, 1)
YEAR_END = date(2027, 7, 10)
ORGANIZATION_TABLES = (
    "organization_operations, student_import_rows, student_import_batches, "
    "class_enrollments, students, administrative_classes, subjects, grade_cohorts, "
    "academic_terms, academic_years"
)
ORG_STUDENTS = "/api/v1/organization/students"


def _is_stub(function) -> bool:
    return "NotImplementedError" in inspect.getsource(function)


STUDENT_IMPORT_ROUTES_ARE_STUB = _is_stub(import_service.list_student_imports) or _is_stub(
    import_service.get_student_import
)


# --------------------------------------------------------------------------------------
# Fixture: dedicated real-PostgreSQL database, real signed RSA tokens, real bootstrap.
# --------------------------------------------------------------------------------------


@pytest.fixture
async def org_api(client, postgres_engine, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    config = Settings(
        _env_file=None,
        auth_issuer="https://identity.invalid",
        auth_audience="huipi-api",
        auth_jwks_url="https://identity.invalid/jwks",
    )
    public = {
        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
        "kid": "fixture",
        "alg": "RS256",
    }
    verifier = TokenVerifier(
        config,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": [public]})),
    )
    monkeypatch.setattr(identity_dependencies, "verifier", verifier)
    original_overrides = app.dependency_overrides.copy()
    # Real JWT/DB authorization replaces the trusted-boundary shortcut from `client`.
    app.dependency_overrides.pop(identity_dependencies.authorize_request)
    app.dependency_overrides.pop(identity_dependencies.get_current_principal)

    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    users: dict[str, User] = {}
    async with factory() as session, session.begin():
        await session.execute(text("TRUNCATE TABLE " + ORGANIZATION_TABLES + " CASCADE"))
        # Append-only audit rows have no DELETE path; the dedicated DB is reset per test.
        await session.execute(text("TRUNCATE TABLE security_audit_events"))
        for name in ("admin", "manager", "importer", "teacher", "outsider", "legacy_admin"):
            row = User(
                issuer=config.auth_issuer,
                subject=str(uuid4()),
                is_active=True,
                is_admin=name in {"admin", "legacy_admin"},
            )
            session.add(row)
            await session.flush()
            users[name] = row

    def principal(name: str = "admin") -> Principal:
        user = users[name]
        return Principal(
            user_id=user.id,
            issuer=user.issuer,
            subject=user.subject,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            is_admin=user.is_admin,
        )

    def token(
        name: str = "admin",
        *,
        signing_key=None,
        kid: str = "fixture",
        claims: dict | None = None,
    ) -> str:
        now = int(time.time())
        payload = {
            "iss": config.auth_issuer,
            "aud": config.auth_audience,
            "sub": users[name].subject,
            "iat": now - 1,
            "exp": now + 600,
            **(claims or {}),
        }
        return jwt.encode(payload, signing_key or key, algorithm="RS256", headers={"kid": kid})

    def headers(name: str = "admin", **claims) -> dict[str, str]:
        return {"Authorization": "Bearer " + token(name, claims=claims or None)}

    class Context:
        pass

    ctx = Context()
    ctx.client = client
    ctx.factory = factory
    ctx.postgres_engine = postgres_engine
    ctx.config = config
    ctx.users = users
    ctx.principal = principal
    ctx.token = token
    ctx.headers = headers
    ctx.verifier = verifier

    try:
        # Real deployment bootstrap: the installation operator can delegate, not self-grant.
        async with factory() as session:
            ctx.bootstrap = await security_service.deployment_bootstrap(
                session, users["admin"].id, uuid4(), "synthetic-approval-ref"
            )
        ctx.manager_grant = await grant_role(
            ctx, "manager", security_service.ORGANIZATION_MANAGER_ROLE
        )
        ctx.importer_grant = await grant_role(
            ctx, "importer", security_service.STUDENT_IMPORTER_ROLE
        )
        yield ctx
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original_overrides)
        ids = [user.id for user in users.values()]
        async with factory() as session, session.begin():
            await session.execute(text("TRUNCATE TABLE " + ORGANIZATION_TABLES + " CASCADE"))
            await session.execute(text("TRUNCATE TABLE security_audit_events"))
            await session.execute(delete(SecurityGrant).where(SecurityGrant.user_id.in_(ids)))
            # Core table delete: the legacy-visibility ORM boundary hides memberships of
            # non-legacy assignments, and this test-operator cleanup removes only own rows.
            await session.execute(
                delete(AssignmentMembership.__table__).where(
                    AssignmentMembership.__table__.c.user_id.in_(ids)
                )
            )
            await session.execute(delete(User).where(User.id.in_(ids)))


# --------------------------------------------------------------------------------------
# Small real-database helpers.
# --------------------------------------------------------------------------------------


async def call(ctx, function, payload, *, principal: str = "manager"):
    async with ctx.factory() as session:
        return await function(session, ctx.principal(principal), payload)


async def expect_access_error(ctx, function, payload, *, principal: str = "manager"):
    async with ctx.factory() as session:
        with pytest.raises(AccessError) as caught:
            await function(session, ctx.principal(principal), payload)
    return caught.value


async def expect_integrity_error(ctx, statement) -> IntegrityError:
    async with ctx.factory() as session:
        with pytest.raises(IntegrityError) as caught:
            await session.execute(statement)
        await session.rollback()
    return caught.value


async def scalar(ctx, statement):
    async with ctx.factory() as session:
        return await session.scalar(statement)


async def scalars(ctx, statement) -> list:
    async with ctx.factory() as session:
        return list((await session.scalars(statement)).all())


async def mappings(ctx, statement) -> list:
    async with ctx.factory() as session:
        return list((await session.execute(statement)).mappings().all())


async def count_rows(ctx, model) -> int:
    return await scalar(ctx, select(func.count()).select_from(model))


async def grant_role(ctx, name: str, role_version_id: UUID) -> dict:
    async with ctx.factory() as session:
        return await security_service.grant_role(
            session,
            ctx.principal("admin"),
            uuid4(),
            user_id=ctx.users[name].id,
            role_version_id=role_version_id,
            scope_id=security_service.INSTALLATION_SCOPE,
        )


async def revoke_grant(ctx, grant_id: UUID) -> dict:
    async with ctx.factory() as session:
        return await security_service.revoke_grant(
            session, ctx.principal("admin"), uuid4(), grant_id=grant_id
        )


async def seed_calendar(ctx, *, year_code="AY-CUR", cohort_code="GC-CUR"):
    async with ctx.factory() as session, session.begin():
        year = AcademicYear(
            code=year_code,
            display_name="合成当前学年",
            start_date=TODAY - timedelta(days=365),
            end_date=TODAY + timedelta(days=365),
            status="active",
        )
        cohort = GradeCohort(
            code=cohort_code,
            display_name="合成当前届别",
            intake_year=TODAY.year - 1,
        )
        session.add_all([year, cohort])
        await session.flush()
        return {"year_id": year.id, "cohort_id": cohort.id}


async def add_class(ctx, cohort_id: UUID, code: str, *, status: str = "active") -> UUID:
    async with ctx.factory() as session, session.begin():
        row = AdministrativeClass(
            grade_cohort_id=cohort_id, code=code, display_name="合成" + code, status=status
        )
        session.add(row)
        await session.flush()
        return row.id


async def add_student(ctx, code: str, name: str) -> UUID:
    async with ctx.factory() as session, session.begin():
        row = Student(school_student_code=code, name=name)
        session.add(row)
        await session.flush()
        return row.id


async def add_enrollment(
    ctx,
    student_id: UUID,
    class_id: UUID,
    valid_from: date,
    valid_to: date | None = None,
) -> UUID:
    async with ctx.factory() as session, session.begin():
        row = ClassEnrollment(
            student_id=student_id,
            administrative_class_id=class_id,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        session.add(row)
        await session.flush()
        return row.id


async def active_grant_ids(ctx, name: str) -> list[UUID]:
    return await scalars(
        ctx,
        select(SecurityGrant.id).where(
            SecurityGrant.user_id == ctx.users[name].id,
            SecurityGrant.revoked_at.is_(None),
        ),
    )


# --------------------------------------------------------------------------------------
# 1. Academic years / terms: trigger, GiST exclusion and unique keys.
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_academic_term_is_bounded_by_year_trigger_and_overlap_exclusion(org_api):
    ctx = org_api
    year = await call(
        ctx,
        org_service.create_academic_year,
        AcademicYearCreate(
            request_id=uuid4(),
            code="AY-2026",
            display_name="合成学年",
            start_date=YEAR_START,
            end_date=YEAR_END,
            status="active",
        ),
    )
    year_id = UUID(year["id"])
    first = await call(
        ctx,
        org_service.create_academic_term,
        AcademicTermCreate(
            request_id=uuid4(),
            academic_year_id=year_id,
            term_no=1,
            code="T1",
            start_date=date(2026, 9, 1),
            end_date=date(2027, 1, 20),
        ),
    )
    assert first["action"] == "academic_term.created"

    # The cross-table trigger is real: direct SQL outside the year is rejected.
    outside = await expect_integrity_error(
        ctx,
        insert(AcademicTerm.__table__).values(
            id=uuid4(),
            academic_year_id=year_id,
            term_no=2,
            code="OUT",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 10, 1),
        ),
    )
    assert "academic_term_outside_year" in str(getattr(outside, "orig", outside))
    outside_end = await expect_integrity_error(
        ctx,
        insert(AcademicTerm.__table__).values(
            id=uuid4(),
            academic_year_id=year_id,
            term_no=3,
            code="OUT2",
            start_date=date(2027, 6, 1),
            end_date=date(2027, 8, 1),
        ),
    )
    assert "academic_term_outside_year" in str(getattr(outside_end, "orig", outside_end))
    missing_year = await expect_integrity_error(
        ctx,
        insert(AcademicTerm.__table__).values(
            id=uuid4(),
            academic_year_id=uuid4(),
            term_no=1,
            code="NOYEAR",
            start_date=date(2026, 10, 1),
            end_date=date(2026, 11, 1),
        ),
    )
    assert "academic_term_year_missing" in str(getattr(missing_year, "orig", missing_year))

    # Date-order check constraints are real at the database layer as well.
    bad_term = await expect_integrity_error(
        ctx,
        insert(AcademicTerm.__table__).values(
            id=uuid4(),
            academic_year_id=year_id,
            term_no=4,
            code="BAD",
            start_date=date(2027, 2, 1),
            end_date=date(2027, 2, 1),
        ),
    )
    assert "ck_academic_terms_date_order" in str(getattr(bad_term, "orig", bad_term))
    bad_year = await expect_integrity_error(
        ctx,
        insert(AcademicYear.__table__).values(
            id=uuid4(),
            code="AY-BAD",
            display_name="合成坏学年",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 1),
        ),
    )
    assert "ck_academic_years_date_order" in str(getattr(bad_year, "orig", bad_year))
    with pytest.raises(ValidationError):
        AcademicYearCreate(
            request_id=uuid4(),
            code="AY-SCHEMA",
            display_name="合成",
            start_date=date(2027, 1, 1),
            end_date=date(2026, 1, 1),
        )
    with pytest.raises(ValidationError):
        AcademicTermCreate(
            request_id=uuid4(),
            academic_year_id=year_id,
            term_no=2,
            code="T2",
            start_date=date(2027, 1, 1),
            end_date=date(2026, 12, 1),
        )

    # Service pre-check returns a stable 422, not a raw driver error.
    outside_service = await expect_access_error(
        ctx,
        org_service.create_academic_term,
        AcademicTermCreate(
            request_id=uuid4(),
            academic_year_id=year_id,
            term_no=2,
            code="T2",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 10, 1),
        ),
    )
    assert outside_service.code == "academic_term_outside_academic_year"
    assert outside_service.status_code == 422

    # Terms in the same academic year may not overlap (GiST exclusion) -> stable 409.
    overlap = await expect_access_error(
        ctx,
        org_service.create_academic_term,
        AcademicTermCreate(
            request_id=uuid4(),
            academic_year_id=year_id,
            term_no=2,
            code="T2",
            start_date=date(2026, 12, 1),
            end_date=date(2027, 2, 1),
        ),
    )
    assert overlap.code == "academic_term_overlap" and overlap.status_code == 409

    # term_no and code are unique inside one academic year -> stable 409 conflict code.
    duplicate_number = await expect_access_error(
        ctx,
        org_service.create_academic_term,
        AcademicTermCreate(
            request_id=uuid4(),
            academic_year_id=year_id,
            term_no=1,
            code="T9",
            start_date=date(2027, 2, 1),
            end_date=date(2027, 3, 1),
        ),
    )
    assert duplicate_number.code == "organization_code_conflict"
    assert duplicate_number.status_code == 409
    duplicate_code = await expect_access_error(
        ctx,
        org_service.create_academic_term,
        AcademicTermCreate(
            request_id=uuid4(),
            academic_year_id=year_id,
            term_no=2,
            code="T1",
            start_date=date(2027, 3, 1),
            end_date=date(2027, 4, 1),
        ),
    )
    assert duplicate_code.code == "organization_code_conflict"
    assert duplicate_code.status_code == 409
    assert await count_rows(ctx, AcademicTerm) == 1
    # Failed service attempts roll back their own audit/idempotency row.
    assert (
        await scalar(
            ctx,
            select(func.count())
            .select_from(OrganizationOperation)
            .where(OrganizationOperation.action == "academic_term.created"),
        )
        == 1
    )


# --------------------------------------------------------------------------------------
# 2. Student identity: names are never keys and codes are unique.
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_same_name_students_stay_distinct_and_student_code_is_unique(org_api):
    ctx = org_api
    first = await add_student(ctx, "S001", "同名合成学生")
    second = await add_student(ctx, "S002", "同名合成学生")
    assert first != second
    rows = await scalars(ctx, select(Student).order_by(Student.school_student_code))
    assert [(row.id, row.school_student_code, row.name) for row in rows] == [
        (first, "S001", "同名合成学生"),
        (second, "S002", "同名合成学生"),
    ]

    page = await maps_students(ctx)
    assert page["has_next"] is False
    assert {record["school_student_code"] for record in page["records"]} == {"S001", "S002"}

    duplicate = await expect_integrity_error(
        ctx,
        insert(Student.__table__).values(id=uuid4(), school_student_code="S001", name="另一个同名"),
    )
    assert "uq_students_school_code" in str(getattr(duplicate, "orig", duplicate))
    assert await count_rows(ctx, Student) == 2

    # There is deliberately no public student-write HTTP route in B-1.1b (two-phase import
    # is the only creation path); a duplicate code therefore cannot be minted via HTTP.
    student_methods = {
        method.upper()
        for path, operations in app.openapi()["paths"].items()
        if path == ORG_STUDENTS
        for method in operations
    }
    assert student_methods == {"GET"}


async def maps_students(ctx) -> dict:
    async with ctx.factory() as session:
        return await org_service.list_students(
            session, class_id=None, query=None, active_only=True, limit=25, offset=0
        )


# --------------------------------------------------------------------------------------
# 3. Homeroom membership: exclusion constraint, transfer history, invalid transfers.
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_class_enrollment_exclusion_and_history_preserving_transfer(org_api):
    ctx = org_api
    calendar = await seed_calendar(ctx)
    class_a = await add_class(ctx, calendar["cohort_id"], "A1")
    class_b = await add_class(ctx, calendar["cohort_id"], "B1")
    class_c = await add_class(ctx, calendar["cohort_id"], "C1")
    inactive_class = await add_class(ctx, calendar["cohort_id"], "D1", status="inactive")
    student = await add_student(ctx, "S001", "合成学生")
    start = TODAY - timedelta(days=90)
    await add_enrollment(ctx, student, class_a, start)

    # Same student, same instant, different homeroom -> PostgreSQL GiST exclusion.
    overlap = await expect_integrity_error(
        ctx,
        insert(ClassEnrollment.__table__).values(
            id=uuid4(),
            student_id=student,
            administrative_class_id=class_b,
            valid_from=start,
            source="manual",
        ),
    )
    assert "ex_class_enrollments_student_time" in str(getattr(overlap, "orig", overlap))
    same_range = await expect_integrity_error(
        ctx,
        insert(ClassEnrollment.__table__).values(
            id=uuid4(),
            student_id=student,
            administrative_class_id=class_a,
            valid_from=start,
            source="manual",
        ),
    )
    assert "ex_class_enrollments_student_time" in str(getattr(same_range, "orig", same_range))
    assert await count_rows(ctx, ClassEnrollment) == 1

    # A transfer is effective from a real date already in force, so history is current.
    effective = TODAY - timedelta(days=5)
    transferred = await call(
        ctx,
        org_service.transfer_student,
        StudentTransferRequest(
            request_id=uuid4(),
            student_id=student,
            to_administrative_class_id=class_b,
            effective_from=effective,
        ),
    )
    assert transferred["from_administrative_class_id"] == str(class_a)
    assert transferred["to_administrative_class_id"] == str(class_b)

    history = await scalars(
        ctx,
        select(ClassEnrollment)
        .where(ClassEnrollment.student_id == student)
        .order_by(ClassEnrollment.valid_from, ClassEnrollment.created_at),
    )
    assert len(history) == 2
    assert history[0].administrative_class_id == class_a
    assert history[0].valid_to == effective
    assert history[1].administrative_class_id == class_b
    assert history[1].valid_from == effective and history[1].valid_to is None
    current = await scalars(
        ctx,
        select(ClassEnrollment).where(
            ClassEnrollment.student_id == student, ClassEnrollment.valid_to.is_(None)
        ),
    )
    assert len(current) == 1 and current[0].administrative_class_id == class_b

    # History remains readable through the service used by HTTP.
    async with ctx.factory() as session:
        page = await org_service.list_student_enrollments(
            session, student_id=student, limit=25, offset=0
        )
    assert [record["administrative_class_id"] for record in page["records"]] == [
        class_b,
        class_a,
    ]
    assert page["records"][1]["valid_to"] == effective

    # Invalid transfers are rejected with stable 409 codes (never raw DB errors).
    same_class = await expect_access_error(
        ctx,
        org_service.transfer_student,
        StudentTransferRequest(
            request_id=uuid4(),
            student_id=student,
            to_administrative_class_id=class_b,
            effective_from=TODAY + timedelta(days=30),
        ),
    )
    assert same_class.code == "student_already_in_class" and same_class.status_code == 409
    not_later = await expect_access_error(
        ctx,
        org_service.transfer_student,
        StudentTransferRequest(
            request_id=uuid4(),
            student_id=student,
            to_administrative_class_id=class_c,
            effective_from=effective,
        ),
    )
    assert not_later.code == "transfer_effective_from_invalid"
    assert not_later.status_code == 409
    inactive_target = await expect_access_error(
        ctx,
        org_service.transfer_student,
        StudentTransferRequest(
            request_id=uuid4(),
            student_id=student,
            to_administrative_class_id=inactive_class,
            effective_from=TODAY + timedelta(days=30),
        ),
    )
    assert inactive_target.code == "administrative_class_unavailable"
    assert inactive_target.status_code == 409
    async with ctx.factory() as session, session.begin():
        await session.execute(update(Student).where(Student.id == student).values(is_active=False))
    inactive_student = await expect_access_error(
        ctx,
        org_service.transfer_student,
        StudentTransferRequest(
            request_id=uuid4(),
            student_id=student,
            to_administrative_class_id=class_c,
            effective_from=TODAY + timedelta(days=30),
        ),
    )
    assert inactive_student.code == "student_inactive"
    assert inactive_student.status_code == 409
    unknown_student = await expect_access_error(
        ctx,
        org_service.transfer_student,
        StudentTransferRequest(
            request_id=uuid4(),
            student_id=uuid4(),
            to_administrative_class_id=class_c,
            effective_from=TODAY + timedelta(days=30),
        ),
    )
    assert unknown_student.code == "resource_not_found_or_forbidden"
    assert unknown_student.status_code == 404
    # No failed attempt added a second current membership.
    assert (
        await scalar(
            ctx,
            select(func.count())
            .select_from(ClassEnrollment)
            .where(ClassEnrollment.student_id == student, ClassEnrollment.valid_to.is_(None)),
        )
        == 1
    )
    assert await count_rows(ctx, ClassEnrollment) == 2


# --------------------------------------------------------------------------------------
# 4. Concurrent transfer of one student: exactly one winner, no 500, one current row.
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrent_transfer_has_one_winner_and_one_current_enrollment(org_api):
    ctx = org_api
    calendar = await seed_calendar(ctx)
    class_a = await add_class(ctx, calendar["cohort_id"], "A1")
    class_b = await add_class(ctx, calendar["cohort_id"], "B1")
    class_c = await add_class(ctx, calendar["cohort_id"], "C1")
    student = await add_student(ctx, "S001", "合成并发学生")
    await add_enrollment(ctx, student, class_a, TODAY - timedelta(days=90))
    effective = TODAY - timedelta(days=5)
    barrier = asyncio.Barrier(2)

    async def move(target_class: UUID):
        async with ctx.factory() as session:
            await barrier.wait()
            return await org_service.transfer_student(
                session,
                ctx.principal("manager"),
                StudentTransferRequest(
                    request_id=uuid4(),
                    student_id=student,
                    to_administrative_class_id=target_class,
                    effective_from=effective,
                ),
            )

    results = await asyncio.gather(move(class_b), move(class_c), return_exceptions=True)
    wins = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(wins) == 1, results
    assert len(failures) == 1, results
    failure = failures[0]
    assert isinstance(failure, AccessError), failure
    assert failure.status_code == 409
    assert failure.code in {
        "transfer_effective_from_invalid",
        "enrollment_overlap",
        "student_already_in_class",
    }

    history = await scalars(
        ctx,
        select(ClassEnrollment)
        .where(ClassEnrollment.student_id == student)
        .order_by(ClassEnrollment.valid_from),
    )
    assert len(history) == 2
    assert history[0].administrative_class_id == class_a
    assert history[0].valid_to == effective
    open_rows = [row for row in history if row.valid_to is None]
    assert len(open_rows) == 1
    assert open_rows[0].administrative_class_id == UUID(wins[0]["to_administrative_class_id"])


# --------------------------------------------------------------------------------------
# 5. Fixed installation-scope authorization matrix with real bootstrap/grant/revoke.
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_organization_read_requires_grant_not_legacy_admin_flag(org_api):
    ctx = org_api
    await add_student(ctx, "S001", "合成学生")

    denied = await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("outsider"))
    assert denied.status_code == 404
    assert denied.json() == {"detail": "resource_not_found_or_forbidden"}
    assert "no-store" in denied.headers["cache-control"]

    allowed = await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("manager"))
    assert allowed.status_code == 200, allowed.text
    assert "no-store" in allowed.headers["cache-control"]
    page = allowed.json()
    assert set(page) == {"records", "limit", "offset", "has_next", "next_offset"}
    assert len(page["records"]) == 1
    assert set(page["records"][0]) == {
        "id",
        "school_student_code",
        "name",
        "is_active",
    }
    assert page["records"][0]["school_student_code"] == "S001"
    assert "created_at" not in allowed.text and "issuer" not in allowed.text

    # A legacy is_admin=True User with no Grant gets nothing: read nor maintenance.
    legacy_read = await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("legacy_admin"))
    assert legacy_read.status_code in {401, 403, 404}
    assert legacy_read.status_code != 500
    assert legacy_read.json()["detail"] in {
        "resource_not_found_or_forbidden",
        "administrator_required",
        "teacher_not_provisioned",
    }
    maintain = await expect_access_error(
        ctx,
        org_service.create_subject,
        SubjectCreate(request_id=uuid4(), code="LGC", display_name="合成科目"),
        principal="legacy_admin",
    )
    assert maintain.code == "resource_not_found_or_forbidden"
    assert maintain.status_code == 404
    assert await count_rows(ctx, Subject) == 0
    assert await count_rows(ctx, OrganizationOperation) == 0

    # Claims inside a token are ignored; only the local User row and Grants authorize.
    forged_claim = await ctx.client.get(
        ORG_STUDENTS, headers=ctx.headers("teacher", role="admin", is_admin=True)
    )
    assert forged_claim.status_code == 404


@pytest.mark.anyio
async def test_teacher_membership_does_not_grant_school_wide_student_read(org_api):
    ctx = org_api
    await add_student(ctx, "S001", "合成学生")
    created = await ctx.client.post(
        "/api/v1/assignments",
        headers=ctx.headers("teacher"),
        json={"title": "合成普通作业", "subject": "数学", "grade_level": "高一"},
    )
    assert created.status_code == 201, created.text
    assignment_id = UUID(created.json()["id"])
    assert (
        await ctx.client.get(f"/api/v1/assignments/{assignment_id}", headers=ctx.headers("teacher"))
    ).status_code == 200
    assert (
        await scalar(
            ctx,
            select(func.count())
            .select_from(AssignmentMembership)
            .where(
                AssignmentMembership.assignment_id == assignment_id,
                AssignmentMembership.user_id == ctx.users["teacher"].id,
            ),
        )
        == 1
    )
    denied = await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("teacher"))
    assert denied.status_code == 404
    assert denied.json() == {"detail": "resource_not_found_or_forbidden"}


@pytest.mark.anyio
async def test_revoke_disable_and_forged_tokens_are_rejected(org_api):
    ctx = org_api
    await add_student(ctx, "S001", "合成学生")
    assert (await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("manager"))).status_code == 200

    await revoke_grant(ctx, UUID(ctx.manager_grant["grant_id"]))
    revoked = await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("manager"))
    assert revoked.status_code == 404
    assert revoked.json() == {"detail": "resource_not_found_or_forbidden"}

    # Re-grant after revoke is allowed and takes effect immediately.
    ctx.manager_grant = await grant_role(ctx, "manager", security_service.ORGANIZATION_MANAGER_ROLE)
    assert (await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("manager"))).status_code == 200

    async with ctx.factory() as session, session.begin():
        await session.execute(
            update(User).where(User.id == ctx.users["manager"].id).values(is_active=False)
        )
    disabled = await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("manager"))
    assert disabled.status_code == 403
    assert disabled.json() == {"detail": "teacher_not_provisioned"}

    # Missing/forged/expired tokens fail closed before any business authorization.
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = [
        (None, "authentication_required"),
        (ctx.token("manager", signing_key=wrong_key), "invalid_token"),
        (ctx.token("manager", kid="unknown-kid"), "invalid_token"),
        (ctx.token("manager", claims={"aud": "other-audience"}), "invalid_token"),
        (ctx.token("manager", claims={"iat": now - 200, "exp": now - 100}), "invalid_token"),
    ]
    for value, expected in forged:
        headers = {} if value is None else {"Authorization": "Bearer " + value}
        response = await ctx.client.get(ORG_STUDENTS, headers=headers)
        assert response.status_code == 401, (expected, response.text)
        assert response.json() == {"detail": expected}
        assert response.headers["www-authenticate"] == "Bearer"
        assert "Bearer " not in response.text


@pytest.mark.anyio
async def test_duplicate_grant_conflict_and_regrant_after_revoke(org_api):
    ctx = org_api
    manager_id = ctx.users["manager"].id
    duplicate_request_id = uuid4()
    async with ctx.factory() as session:
        with pytest.raises(AccessError) as caught:
            await security_service.grant_role(
                session,
                ctx.principal("admin"),
                duplicate_request_id,
                user_id=manager_id,
                role_version_id=security_service.ORGANIZATION_MANAGER_ROLE,
                scope_id=security_service.INSTALLATION_SCOPE,
            )
    assert caught.value.code == "security_grant_already_active"
    assert caught.value.status_code == 409
    assert len(await active_grant_ids(ctx, "manager")) == 1
    # The failed duplicate grant is fully rolled back (no audit fact, one active Grant).
    assert (
        await scalar(
            ctx,
            select(func.count())
            .select_from(SecurityAudit)
            .where(SecurityAudit.request_id == duplicate_request_id),
        )
        == 0
    )

    await revoke_grant(ctx, UUID(ctx.manager_grant["grant_id"]))
    assert len(await active_grant_ids(ctx, "manager")) == 0
    assert (await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("manager"))).status_code == 404
    ctx.manager_grant = await grant_role(ctx, "manager", security_service.ORGANIZATION_MANAGER_ROLE)
    assert len(await active_grant_ids(ctx, "manager")) == 1
    assert (await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("manager"))).status_code == 200


@pytest.mark.anyio
async def test_unregistered_route_method_and_name_are_not_authorized(org_api):
    ctx = org_api
    # Starlette rejects an undeclared method before route dependencies run: it is still
    # rejected (405) and never executes; the fixed-policy guard owns name/method mismatch.
    method_mismatch = await ctx.client.put(ORG_STUDENTS, headers=ctx.headers("manager"))
    assert method_mismatch.status_code in {403, 405}, method_mismatch.text
    if method_mismatch.status_code == 403:
        assert method_mismatch.json() == {"detail": "route_not_authorized"}
    else:
        assert "no-store" not in method_mismatch.headers
        assert method_mismatch.json() == {"detail": "Method Not Allowed"}

    from fastapi import Depends, FastAPI

    shadow = FastAPI()
    shadow.dependency_overrides.update(app.dependency_overrides)
    shadow.add_exception_handler(AccessError, app.exception_handlers[AccessError])

    @shadow.post(
        ORG_STUDENTS,
        name="list_students",
        dependencies=[Depends(identity_dependencies.authorize_request)],
    )
    async def wrong_method_route():
        raise AssertionError("unregistered method must never execute")

    @shadow.get(
        ORG_STUDENTS,
        name="organization_shadow_route",
        dependencies=[Depends(identity_dependencies.authorize_request)],
    )
    async def unregistered_policy_route():
        raise AssertionError("unregistered policy must never execute")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shadow), base_url="http://test"
    ) as shadow_client:
        by_method = await shadow_client.post(ORG_STUDENTS, headers=ctx.headers("manager"))
        by_name = await shadow_client.get(ORG_STUDENTS, headers=ctx.headers("manager"))
    assert by_method.status_code == 403
    assert by_method.json() == {"detail": "route_not_authorized"}
    assert by_name.status_code == 403
    assert by_name.json() == {"detail": "route_not_authorized"}


# --------------------------------------------------------------------------------------
# 6. HTTP contract: pagination boundaries, stable sorting, unknown resources, OpenAPI.
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pagination_limits_sorting_and_unknown_resources(org_api):
    ctx = org_api
    for code in ("S010", "S002", "S001"):
        await add_student(ctx, code, "合成" + code)

    first_page = await ctx.client.get(
        ORG_STUDENTS, headers=ctx.headers("manager"), params={"limit": 2, "offset": 0}
    )
    assert first_page.status_code == 200
    assert [row["school_student_code"] for row in first_page.json()["records"]] == [
        "S001",
        "S002",
    ]
    assert first_page.json()["has_next"] is True
    assert first_page.json()["next_offset"] == 2
    second_page = await ctx.client.get(
        ORG_STUDENTS, headers=ctx.headers("manager"), params={"limit": 2, "offset": 2}
    )
    assert [row["school_student_code"] for row in second_page.json()["records"]] == ["S010"]
    assert second_page.json()["has_next"] is False
    assert second_page.json()["next_offset"] is None
    # Stable ordering across repeated identical requests.
    repeat = await ctx.client.get(
        ORG_STUDENTS, headers=ctx.headers("manager"), params={"limit": 2, "offset": 0}
    )
    assert repeat.json()["records"] == first_page.json()["records"]

    for index, params in enumerate(
        [
            {"limit": 0},
            {"limit": 101},
            {"offset": -1},
            {"offset": 1000001},
        ]
    ):
        response = await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("manager"), params=params)
        assert response.status_code == 422, (index, params, response.text)

    unknown_student = await ctx.client.get(
        f"{ORG_STUDENTS}/{uuid4()}/enrollments", headers=ctx.headers("manager")
    )
    assert unknown_student.status_code == 404
    assert unknown_student.json() == {"detail": "resource_not_found_or_forbidden"}


@pytest.mark.skipif(
    STUDENT_IMPORT_ROUTES_ARE_STUB,
    reason="student import service still owned/in-flight by the import workstream",
)
@pytest.mark.anyio
async def test_student_import_history_empty_page_and_unknown_batch(org_api):
    ctx = org_api
    history = await ctx.client.get(
        "/api/v1/organization/student-imports", headers=ctx.headers("manager")
    )
    assert history.status_code == 200, history.text
    assert history.json()["records"] == []
    assert history.json()["has_next"] is False
    unknown = await ctx.client.get(
        f"/api/v1/organization/student-imports/{uuid4()}", headers=ctx.headers("manager")
    )
    assert unknown.status_code == 404


def test_openapi_and_permission_inventory_cover_eleven_organization_operations():
    expected = {
        ("GET", "/api/v1/organization/academic-years"),
        ("GET", "/api/v1/organization/academic-terms"),
        ("GET", "/api/v1/organization/grade-cohorts"),
        ("GET", "/api/v1/organization/subjects"),
        ("GET", "/api/v1/organization/classes"),
        ("GET", "/api/v1/organization/students"),
        ("GET", "/api/v1/organization/students/{student_id}/enrollments"),
        ("GET", "/api/v1/organization/student-imports"),
        ("GET", "/api/v1/organization/student-imports/{batch_id}"),
        ("POST", "/api/v1/organization/student-imports"),
        ("POST", "/api/v1/organization/student-imports/{batch_id}/confirm"),
    }
    actual = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/v1/organization/")
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    }
    inventory = json.loads(
        (PROJECT_ROOT / "docs/security/http-permissions.json").read_text(encoding="utf-8")
    )
    documented = {
        (row["method"], row["path"])
        for row in inventory["operations"]
        if row["path"].startswith("/api/v1/organization/")
    }
    assert len(expected) == 11
    assert actual == expected
    assert documented == expected
    rows = {
        row["policy"]: row
        for row in inventory["operations"]
        if row["path"].startswith("/api/v1/organization/")
    }
    assert rows
    for policy, row in rows.items():
        assert policy in identity_dependencies.ORGANIZATION_POLICIES
        assert identity_dependencies.ORGANIZATION_POLICIES[policy] == row["permission"]
        assert identity_dependencies.POLICY_PATHS[policy] == row["path"][len("/api/v1") :]
        assert row["permission"] in identity_dependencies.ORGANIZATION_POLICIES.values()
    # The fixed policy map is exactly the 11 organization route names.
    assert set(rows) == set(identity_dependencies.ORGANIZATION_POLICIES)


# --------------------------------------------------------------------------------------
# 7. Database faults: stable 503, no half-written data, unknown commit replay.
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_audit_insert_failure_returns_stable_503_without_residue(org_api, postgres_engine):
    ctx = org_api
    request_id = uuid4()

    def fail_organization_audit(conn, cursor, statement, params, context, executemany):
        if "INSERT INTO organization_operations" in statement:
            raise OperationalError("audit insert", {}, Exception("synthetic unavailable"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail_organization_audit)
    try:
        error = await expect_access_error(
            ctx,
            org_service.create_subject,
            SubjectCreate(request_id=request_id, code="FAIL-1", display_name="合成失败科目"),
        )
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail_organization_audit)
    assert error.code == "organization_operation_unavailable"
    assert error.status_code == 503
    assert await count_rows(ctx, Subject) == 0
    assert (
        await scalar(
            ctx,
            select(func.count())
            .select_from(OrganizationOperation)
            .where(OrganizationOperation.request_id == request_id),
        )
        == 0
    )


@pytest.mark.anyio
async def test_read_path_database_failure_returns_stable_503(org_api, postgres_engine):
    ctx = org_api

    def fail_student_reads(conn, cursor, statement, params, context, executemany):
        if "FROM students" in statement:
            raise OperationalError("read", {}, Exception("synthetic unavailable"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail_student_reads)
    try:
        response = await ctx.client.get(ORG_STUDENTS, headers=ctx.headers("manager"))
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail_student_reads)
    assert response.status_code == 503
    assert response.json() == {"detail": "organization_operation_unavailable"}
    assert "synthetic" not in response.text and "SELECT" not in response.text
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.anyio
async def test_lost_commit_ack_is_unknown_then_replays(org_api, monkeypatch):
    ctx = org_api
    request_id = uuid4()
    payload = SubjectCreate(request_id=request_id, code="LOST-1", display_name="合成功亏一篑科目")
    commit = ctx.factory.class_.commit

    async def lost_ack(session):
        await commit(session)
        if session.info.pop("lost_ack", False):
            raise OSError("synthetic commit ack lost")

    monkeypatch.setattr(ctx.factory.class_, "commit", lost_ack)
    async with ctx.factory() as session:
        session.info["lost_ack"] = True
        with pytest.raises(AccessError) as caught:
            await org_service.create_subject(session, ctx.principal("manager"), payload)
    assert caught.value.code == "organization_commit_outcome_unknown"
    assert caught.value.status_code == 503

    recovered = await call(
        ctx,
        org_service.create_subject,
        SubjectCreate(request_id=request_id, code="LOST-1", display_name="合成功亏一篑科目"),
    )
    rows = await scalars(ctx, select(Subject).where(Subject.code == "LOST-1"))
    assert len(rows) == 1 and str(rows[0].id) == recovered["id"]
    assert (
        await scalar(
            ctx,
            select(func.count())
            .select_from(OrganizationOperation)
            .where(OrganizationOperation.request_id == request_id),
        )
        == 1
    )

    conflict = await expect_access_error(
        ctx,
        org_service.create_subject,
        SubjectCreate(request_id=request_id, code="LOST-2", display_name="合成另一科目"),
    )
    assert conflict.code == "organization_request_conflict"
    assert conflict.status_code == 409
    assert await count_rows(ctx, Subject) == 1


# --------------------------------------------------------------------------------------
# 8. Legacy system regression: old HTTP paths and unique keys are unaffected.
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_legacy_assignment_paths_work_and_protected_data_stays_hidden(org_api):
    ctx = org_api
    created = await ctx.client.post(
        "/api/v1/assignments",
        headers=ctx.headers("teacher"),
        json={"title": "合成普通作业", "subject": "数学", "grade_level": "高一"},
    )
    assert created.status_code == 201, created.text
    legacy_id = UUID(created.json()["id"])
    listing = await ctx.client.get("/api/v1/assignments", headers=ctx.headers("teacher"))
    assert listing.status_code == 200
    assert [row["id"] for row in listing.json()] == [str(legacy_id)]
    detail = await ctx.client.get(
        f"/api/v1/assignments/{legacy_id}", headers=ctx.headers("teacher")
    )
    assert detail.status_code == 200
    assert detail.json()["title"] == "合成普通作业"
    # The legacy HTTP write path still stores the fixed legacy classification.
    assert (
        await scalar(
            ctx,
            text("SELECT security_classification FROM assignments WHERE id = :id").bindparams(
                id=legacy_id
            ),
        )
        == "legacy_homework"
    )
    # Another authenticated user without a membership cannot read it.
    assert (
        await ctx.client.get(f"/api/v1/assignments/{legacy_id}", headers=ctx.headers("manager"))
    ).status_code == 404

    protected_id = uuid4()
    async with ctx.factory() as session, session.begin():
        await session.execute(
            insert(Assignment.__table__).values(
                id=protected_id,
                title="SYNTHETIC-PROTECTED",
                subject="数学",
                grade_level="高一",
                security_classification="exam_protected",
                status="draft",
            )
        )
        session.add(
            AssignmentMembership(
                assignment_id=protected_id, user_id=ctx.users["teacher"].id, can_write=True
            )
        )
    teacher_listing = await ctx.client.get("/api/v1/assignments", headers=ctx.headers("teacher"))
    assert str(protected_id) not in teacher_listing.text
    for who in ("teacher", "admin"):
        hidden = await ctx.client.get(
            f"/api/v1/assignments/{protected_id}", headers=ctx.headers(who)
        )
        assert hidden.status_code == 404
        assert hidden.json() == {"detail": "resource_not_found_or_forbidden"}
        assert b"SYNTHETIC-PROTECTED" not in hidden.content

    # Pre-existing unique keys are untouched by the organization tables.
    constraint_names = set(
        await scalars(
            ctx,
            text(
                "SELECT conname FROM pg_constraint WHERE conname IN ("
                "'uq_submissions_assignment_student_ref',"
                "'uq_questions_assignment_number',"
                "'uq_assignment_memberships_user',"
                "'uq_students_school_code')"
            ),
        )
    )
    assert constraint_names == {
        "uq_submissions_assignment_student_ref",
        "uq_questions_assignment_number",
        "uq_assignment_memberships_user",
        "uq_students_school_code",
    }
    async with ctx.factory() as session, session.begin():
        await session.execute(
            insert(Submission.__table__).values(
                id=uuid4(), assignment_id=legacy_id, student_ref="synthetic-one"
            )
        )
    duplicate_submission = await expect_integrity_error(
        ctx,
        insert(Submission.__table__).values(
            id=uuid4(), assignment_id=legacy_id, student_ref="synthetic-one"
        ),
    )
    assert "uq_submissions_assignment_student_ref" in str(
        getattr(duplicate_submission, "orig", duplicate_submission)
    )
    assert (
        await scalar(
            ctx,
            select(func.count())
            .select_from(Submission)
            .where(Submission.assignment_id == legacy_id),
        )
        == 1
    )
