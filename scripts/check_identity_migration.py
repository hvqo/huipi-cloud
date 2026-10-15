"""Create/drop ONLY a new UUID test DB. Never migrate the configured development DB."""

import asyncio
import os
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from huipi_cloud.core.config import Settings


async def main():
    config = Settings()
    if not config.test_database_url:
        raise RuntimeError("TEST_DATABASE_URL is required")
    url = make_url(config.test_database_url)
    if not url.database.endswith("_test") or url.database == make_url(config.database_url).database:
        raise RuntimeError("requires a separate test database")
    name = f"huipi_identity_{uuid4().hex}_test"
    target = url.set(database=name)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    created = False
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        env = dict(os.environ, DATABASE_URL=target.render_as_string(hide_password=False))

        async def alembic(*args):
            # Move subprocess wait off the event loop. DSN never appears in argv or output.
            await asyncio.to_thread(
                subprocess.run, [sys.executable, "-m", "alembic", *args], env=env, check=True
            )

        await alembic("upgrade", "0fd471e9f785")
        engine = create_async_engine(target)
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                INSERT INTO assignments (id, title, subject, grade_level)
                VALUES (:id, 'synthetic migration sentinel', 'math', '10')
            """),
                {"id": uuid4()},
            )
        await engine.dispose()
        await alembic("upgrade", "head")
        await alembic("check")
        engine = create_async_engine(target)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM assignments")) == 1
            assert await connection.scalar(text("SELECT count(*) FROM assignment_memberships")) == 0
        await engine.dispose()
        await alembic("downgrade", "0fd471e9f785")
        engine = create_async_engine(target)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM assignments")) == 1
            assert await connection.scalar(text("SELECT to_regclass('users')")) is None
        await engine.dispose()
        await alembic("upgrade", "head")
        await alembic("check")
        print(
            "identity migration: upgrade/check/downgrade/re-upgrade/check PASS; legacy row retained"
        )
    finally:
        if created:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


if __name__ == "__main__":
    asyncio.run(main())
