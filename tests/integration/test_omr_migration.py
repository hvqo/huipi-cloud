"""Migration roundtrip uses a newly-created isolated PostgreSQL database."""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_organization_migration import alembic


@pytest.mark.anyio
async def test_omr_migration_upgrade_check_downgrade_and_reupgrade(postgres_engine):
    name = "huipi_omr_roundtrip_" + uuid4().hex[:10] + "_test"
    url = postgres_engine.url.set(database=name)
    admin = create_async_engine(
        postgres_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin.connect() as connection:
            await connection.execute(text('CREATE DATABASE "' + name + '"'))
        await alembic(url, "upgrade", "b16a82e6a017")
        await alembic(url, "upgrade", "head")
        await alembic(url, "check")
        await alembic(url, "downgrade", "b16a82e6a017")
        await alembic(url, "upgrade", "head")
        await alembic(url, "check")
    finally:
        async with admin.connect() as connection:
            await connection.execute(text('DROP DATABASE IF EXISTS "' + name + '" WITH (FORCE)'))
        await admin.dispose()
