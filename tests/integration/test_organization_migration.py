"""B-1.1b independent migration roundtrip on a throwaway PostgreSQL database.

The migration is exercised outside the shared test database: a fresh temporary database is
created from the ``TEST_DATABASE_URL`` account, upgraded to the B-1.1a revision, seeded
with a real synthetic User/Assignment/Submission/ParsingTask graph, then upgraded to head.
Old rows must be byte-for-byte unchanged, ``alembic check`` must be clean and the new
constraints/triggers must exist. The destructive downgrade must refuse as soon as either
organization rows or one of the new role Grants exist.
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import create_async_engine

from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.identity.models import User
from huipi_cloud.modules.organization.models import AcademicYear, Student
from huipi_cloud.modules.resource_security.models import SecurityGrant
from huipi_cloud.modules.submissions.models import ParsingTask, Submission

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASE_REVISION = "e21a9b4c110a"
HEAD_REVISION = "b14b41a2e903"
OLD_TABLES = ("users", "assignments", "submissions", "parsing_tasks")
INSTALLATION_SCOPE = UUID("11111111-b11a-4000-8000-000000000001")
ORGANIZATION_MANAGER_ROLE = UUID("11111111-b11b-4000-8000-000000000002")
ORGANIZATION_PERMISSIONS = (
    "organization.read",
    "organization.manage",
    "student.import.preview",
    "student.import.confirm",
)

CONSTRAINTS = {
    # GiST exclusion constraints live in pg_constraint.
    "ex_academic_terms_year_range",
    "ex_class_enrollments_student_time",
    "uq_academic_years_code",
    # B-1.1a active-Grant partial unique index (an index, not a constraint).
    "uq_security_grants_active",
}
OLD_UNIQUE_CONSTRAINTS = {
    "uq_submissions_assignment_student_ref",
    "uq_questions_assignment_number",
}
TRIGGERS = {
    "valid_academic_term_range",
    "immutable_organization_operations",
    "immutable_student_import_rows",
    "protect_student_import_batch",
    "protect_class_enrollment_history",
}
NEW_TABLES = {
    "academic_years",
    "academic_terms",
    "grade_cohorts",
    "subjects",
    "administrative_classes",
    "students",
    "class_enrollments",
    "student_import_batches",
    "student_import_rows",
    "organization_operations",
}


async def alembic(url, *arguments, success: bool = True):
    environment = dict(os.environ, DATABASE_URL=url.render_as_string(hide_password=False))
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    if success:
        assert result.returncode == 0, (arguments, result.stderr[-2000:])
    return result


async def snapshot(url) -> dict:
    """Counts and content digests of the pre-B-1.1b tables."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            result = {}
            for name in OLD_TABLES:
                rows = (
                    (await connection.execute(text('SELECT * FROM "' + name + '" ORDER BY 1')))
                    .mappings()
                    .all()
                )
                payload = [{key: value for key, value in row.items()} for row in rows]
                result[name] = {
                    "count": len(rows),
                    "digest": hashlib.sha256(
                        json.dumps(payload, sort_keys=True, default=str).encode()
                    ).hexdigest(),
                }
            return result
    finally:
        await engine.dispose()


async def query_all(url, statement):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(statement)).all()
    finally:
        await engine.dispose()


async def execute_sql(url, statements) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(statement)
    finally:
        await engine.dispose()


async def migration_state(url) -> tuple[str, set[str], set[str]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            constraints = set(
                await connection.scalars(
                    text("SELECT conname FROM pg_constraint WHERE conname = ANY(:names)"),
                    {"names": sorted(CONSTRAINTS | OLD_UNIQUE_CONSTRAINTS)},
                )
            )
            indexes = set(
                await connection.scalars(
                    text("SELECT indexname FROM pg_indexes WHERE indexname = ANY(:names)"),
                    {"names": sorted(CONSTRAINTS)},
                )
            )
            triggers = set(
                await connection.scalars(
                    text(
                        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal "
                        "AND tgname = ANY(:names)"
                    ),
                    {"names": sorted(TRIGGERS)},
                )
            )
            return revision, constraints | indexes, triggers
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_independent_organization_migration_roundtrip_and_guarded_downgrade(
    postgres_engine,
):
    base_url = postgres_engine.url
    name = "huipi_b11b_roundtrip_" + uuid4().hex[:10] + "_test"
    url = base_url.set(database=name)
    admin = create_async_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    created = False
    try:
        async with admin.connect() as connection:
            await connection.execute(text('CREATE DATABASE "' + name + '"'))
        created = True

        # 1) Upgrade only to the B-1.1a revision and seed a real old-system graph.
        await alembic(url, "upgrade", BASE_REVISION)
        user_id, assignment_id, submission_id, parsing_task_id = (
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
        )
        await execute_sql(
            url,
            [
                insert(User.__table__).values(
                    id=user_id,
                    issuer="https://migration-tests.invalid",
                    subject=str(uuid4()),
                    is_active=True,
                    is_admin=False,
                ),
                insert(Assignment.__table__).values(
                    id=assignment_id,
                    title="合成迁移作业",
                    subject="数学",
                    grade_level="高一",
                    status="draft",
                ),
                insert(Submission.__table__).values(
                    id=submission_id,
                    assignment_id=assignment_id,
                    student_ref="synthetic-migration-student",
                    status="submitted",
                ),
                insert(ParsingTask.__table__).values(
                    id=parsing_task_id,
                    submission_id=submission_id,
                    status="pending",
                ),
            ],
        )
        before = await snapshot(url)
        assert before["users"]["count"] == 1
        assert before["assignments"]["count"] == 1
        assert before["submissions"]["count"] == 1
        assert before["parsing_tasks"]["count"] == 1

        # 2) Upgrade to head: the old graph is preserved and the model is in sync.
        await alembic(url, "upgrade", "head")
        await alembic(url, "check")
        assert await snapshot(url) == before
        revision, constraints, triggers = await migration_state(url)
        assert revision == HEAD_REVISION
        assert constraints == CONSTRAINTS | OLD_UNIQUE_CONSTRAINTS, constraints
        assert triggers == TRIGGERS, triggers
        scope_rows = await query_all(
            url,
            text("SELECT count(*) FROM security_scopes WHERE id = :scope_id").bindparams(
                scope_id=INSTALLATION_SCOPE
            ),
        )
        # The B-1.1b migration pre-seeds the single installation scope.
        assert scope_rows == [(1,)]
        table_rows = await query_all(
            url,
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"),
        )
        tables = {row[0] for row in table_rows}
        assert NEW_TABLES <= tables
        role_rows = await query_all(
            url,
            text(
                "SELECT role_code FROM security_role_versions WHERE role_code = ANY("
                "ARRAY['organization_operator','organization_manager','student_importer'])"
            ),
        )
        assert {row[0] for row in role_rows} == {
            "organization_operator",
            "organization_manager",
            "student_importer",
        }
        permission_rows = await query_all(
            url,
            text(
                "SELECT code, enabled FROM permission_definitions WHERE code = ANY(:codes)"
            ).bindparams(codes=list(ORGANIZATION_PERMISSIONS)),
        )
        assert {row[0] for row in permission_rows} == set(ORGANIZATION_PERMISSIONS)
        assert all(row[1] is True for row in permission_rows)

        # 3) With empty organization data the downgrade is allowed and reversible.
        await alembic(url, "downgrade", BASE_REVISION)
        state = await query_all(url, text("SELECT version_num FROM alembic_version"))
        assert state == [(BASE_REVISION,)]
        assert await snapshot(url) == before
        await alembic(url, "upgrade", "head")
        await alembic(url, "check")
        assert await snapshot(url) == before
        remaining_roles = await query_all(
            url,
            text(
                "SELECT count(*) FROM security_role_versions WHERE role_code IN "
                "('organization_operator','organization_manager','student_importer')"
            ),
        )
        assert remaining_roles == [(3,)]

        # 4) Organization rows make the destructive downgrade refuse and roll back.
        await execute_sql(
            url,
            [
                insert(AcademicYear.__table__).values(
                    id=uuid4(),
                    code="AY-DOWNGRADE",
                    display_name="合成学年",
                    start_date=date(2026, 9, 1),
                    end_date=date(2027, 7, 10),
                    status="active",
                ),
                insert(Student.__table__).values(
                    id=uuid4(),
                    school_student_code="S-DOWNGRADE",
                    name="合成学生",
                    is_active=True,
                ),
            ],
        )
        rejected = await alembic(url, "downgrade", BASE_REVISION, success=False)
        assert rejected.returncode != 0
        assert "organization_downgrade_requires_archived_new_data" in rejected.stderr
        revision, _, _ = await migration_state(url)
        assert revision == HEAD_REVISION  # transactional downgrade did not half-apply
        tables_after_failed = {
            row[0]
            for row in await query_all(
                url, text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        assert "organization_operations" in tables_after_failed
        await execute_sql(
            url,
            [
                text("DELETE FROM students"),
                text("DELETE FROM academic_years"),
            ],
        )

        # 5) One active Grant of a B-1.1b role also blocks the destructive downgrade.
        grant_id = uuid4()
        await execute_sql(
            url,
            [
                insert(SecurityGrant.__table__).values(
                    id=grant_id,
                    user_id=user_id,
                    role_version_id=ORGANIZATION_MANAGER_ROLE,
                    scope_id=INSTALLATION_SCOPE,
                ),
            ],
        )
        rejected_grant = await alembic(url, "downgrade", BASE_REVISION, success=False)
        assert rejected_grant.returncode != 0
        assert "organization_downgrade_requires_archived_new_data" in rejected_grant.stderr
        revision, _, _ = await migration_state(url)
        assert revision == HEAD_REVISION
        await execute_sql(
            url,
            [
                text("DELETE FROM security_grants WHERE id = :grant_id").bindparams(
                    grant_id=grant_id
                ),
            ],
        )
        assert await snapshot(url) == before
    finally:
        if created:
            async with admin.connect() as connection:
                await connection.execute(text('DROP DATABASE IF EXISTS "' + name + '"'))
        await admin.dispose()


def migration_head():
    """Expected current head, independently resolved from the migration graph."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1
    return heads[0]
