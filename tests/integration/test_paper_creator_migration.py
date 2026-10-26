"""Isolated migration roundtrip with real pre-policy paper/assignment history."""
# ruff: noqa: F401, F811

import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from test_organization_migration import alembic
from test_paper_versions import auth_api, create, papers

from huipi_cloud.infrastructure.database.base import Base
from huipi_cloud.modules.papers.permissions import PAPER_AUTHOR_ROLE, PAPER_CREATOR_ROLE
from huipi_cloud.modules.resource_security.service import INSTALLATION_SCOPE


@pytest.mark.anyio
async def test_creator_migration_retains_old_history_no_backfill_roundtrip_and_refuses_facts(
    papers,
    postgres_engine,
):
    ctx = papers
    await create(ctx)
    names = [
        "users",
        "assignments",
        "assignment_memberships",
        "questions",
        "answer_keys",
        "rubric_criteria",
        "paper_families",
        "paper_versions",
        "paper_question_mappings",
        "security_scopes",
        "security_grants",
        "paper_operations",
    ]
    async with ctx.factory() as db:
        rows = {
            name: [
                dict(r) for r in (await db.execute(select(Base.metadata.tables[name]))).mappings()
            ]
            for name in names
        }
    rows["security_scopes"] = [r for r in rows["security_scopes"] if r["kind"] != "installation"]
    rows["security_grants"] = [
        r
        for r in rows["security_grants"]
        if r["role_version_id"] not in {PAPER_CREATOR_ROLE, PAPER_AUTHOR_ROLE}
    ]
    name = "huipi_creator_roundtrip_" + uuid4().hex[:10] + "_test"
    url = postgres_engine.url.set(database=name)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    target = create_async_engine(url)

    def digest(data):
        return hashlib.sha256(
            json.dumps(
                sorted(data, key=lambda r: json.dumps(r, sort_keys=True, default=str)),
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()

    try:
        async with admin.connect() as db:
            await db.execute(text('CREATE DATABASE "' + name + '"'))
        await alembic(url, "upgrade", "b15a60c4f015")
        async with target.begin() as db:
            for table in Base.metadata.sorted_tables:
                if rows.get(table.name):
                    await db.execute(insert(table), rows[table.name])

        async def snapshot():
            async with target.connect() as db:
                return {
                    table: digest(
                        [
                            dict(r)
                            for r in (
                                await db.execute(text('SELECT * FROM "' + table + '"'))
                            ).mappings()
                        ]
                    )
                    for table in names
                }

        async def old_roles():
            async with target.connect() as db:
                return {
                    table: digest(
                        [
                            dict(r)
                            for r in (
                                await db.execute(
                                    text(
                                        f"SELECT * FROM {table} WHERE {column} "
                                        "NOT IN (:creator,:author)"
                                    ),
                                    dict(creator=PAPER_CREATOR_ROLE, author=PAPER_AUTHOR_ROLE),
                                )
                            ).mappings()
                        ]
                    )
                    for table, column in [
                        ("security_role_versions", "id"),
                        ("security_role_permissions", "role_version_id"),
                    ]
                }

        before = await snapshot()
        roles_before = await old_roles()
        for command in [("upgrade", "head"), ("downgrade", "b15a60c4f015"), ("upgrade", "head")]:
            await alembic(url, *command)
            assert await snapshot() == before
            assert await old_roles() == roles_before
            if command[0] == "upgrade":
                await alembic(url, "check")
                async with target.connect() as db:
                    assert (
                        await db.scalar(text("SELECT count(*) FROM paper_creator_authorities")) == 0
                    )
                    assert (
                        await db.scalar(text("SELECT version_num FROM alembic_version"))
                        == "b15b71d5f016"
                    )
        # Genuine new role fact is retained even if revoked; destructive downgrade must refuse.
        async with target.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO security_grants(id,user_id,role_version_id,scope_id,revoked_at) "
                    "VALUES (:id,:user,:role,:scope,now())"
                ),
                dict(
                    id=uuid4(),
                    user=ctx.users["a"].id,
                    role=PAPER_CREATOR_ROLE,
                    scope=INSTALLATION_SCOPE,
                ),
            )
        rejected = await alembic(url, "downgrade", "b15a60c4f015", success=False)
        assert (
            rejected.returncode != 0 and "paper_creator_facts_prevent_downgrade" in rejected.stderr
        )
        async with target.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version")) == "b15b71d5f016"
            )
    finally:
        await target.dispose()
        async with admin.connect() as db:
            await db.execute(text('DROP DATABASE IF EXISTS "' + name + '"'))
        await admin.dispose()
