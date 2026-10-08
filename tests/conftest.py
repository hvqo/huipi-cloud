"""Shared real-PostgreSQL integration-test fixtures."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from huipi_cloud.core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def migrated_test_database_url() -> str:
    settings = Settings()
    database_url = settings.test_database_url
    if not database_url:
        pytest.skip("设置 TEST_DATABASE_URL 并启动 PostgreSQL 后才运行数据库集成测试")

    test_database = make_url(database_url)
    if not settings.database_url:
        raise RuntimeError("数据库集成测试还需要设置应用 DATABASE_URL")
    application_database = make_url(settings.database_url)
    if not test_database.database or not test_database.database.endswith("_test"):
        raise RuntimeError("TEST_DATABASE_URL 必须指向名称以 _test 结尾的专用数据库")
    if test_database.database == application_database.database:
        raise RuntimeError("TEST_DATABASE_URL 不能指向应用开发数据库")

    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        text=True,
    )
    return database_url


@pytest.fixture
async def postgres_engine(
    anyio_backend: str,
    migrated_test_database_url: str,
) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_test_database_url, pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()
