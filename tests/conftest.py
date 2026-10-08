"""Shared real-PostgreSQL integration-test fixtures."""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage
from huipi_cloud.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def migrated_test_database_url() -> str:
    settings = Settings()
    database_url = settings.test_database_url
    if not database_url:
        raise RuntimeError("集成测试必须设置 TEST_DATABASE_URL 并使用真实 PostgreSQL")

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


@pytest.fixture
async def client(
    anyio_backend: str,
    postgres_engine: AsyncEngine,
) -> AsyncIterator[httpx.AsyncClient]:
    """Use the dedicated migrated PostgreSQL test database for API requests."""
    async with postgres_engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE assignments CASCADE"))

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db_session] = override_session
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        async with postgres_engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE assignments CASCADE"))


class RecordingS3Storage:
    """Record exact keys while delegating every operation to real MinIO."""

    def __init__(self, storage: S3ObjectStorage) -> None:
        self.storage = storage
        self.bucket = storage.bucket
        self.object_key_prefix = storage.object_key_prefix
        self.uploaded_keys: set[str] = set()
        self.after_upload_barrier: asyncio.Barrier | None = None

    async def upload_fileobj(self, fileobj, object_key: str, content_type: str) -> None:
        await self.storage.upload_fileobj(fileobj, object_key, content_type)
        self.uploaded_keys.add(object_key)
        if self.after_upload_barrier is not None:
            await asyncio.wait_for(self.after_upload_barrier.wait(), timeout=10)

    async def download(self, object_key: str):
        return await self.storage.download(object_key)

    async def delete(self, object_key: str) -> None:
        await self.storage.delete(object_key)

    async def cleanup(self) -> None:
        for object_key in self.uploaded_keys:
            await self.storage.delete(object_key)


@pytest.fixture
async def recording_minio_storage(anyio_backend: str) -> AsyncIterator[RecordingS3Storage]:
    """Use an isolated local MinIO-compatible bucket and unique per-test key prefix."""
    settings = Settings()
    if not (
        settings.minio_endpoint_url
        and settings.minio_access_key
        and settings.minio_secret_key
    ):
        raise RuntimeError("对象存储集成测试必须设置本地 S3 兼容服务环境变量")
    endpoint = urlparse(settings.minio_endpoint_url)
    if endpoint.hostname not in {"127.0.0.1", "localhost", "minio"}:
        raise RuntimeError("MinIO 集成测试只允许连接本地端点")
    if settings.minio_test_bucket == settings.minio_bucket:
        raise RuntimeError("MINIO_TEST_BUCKET 必须与开发存储桶分离")

    storage = S3ObjectStorage(
        endpoint_url=settings.minio_endpoint_url,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_test_bucket,
        object_key_prefix=f"tests/{uuid4()}",
    )
    try:
        storage._client.head_bucket(Bucket=storage.bucket)
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        storage._client.create_bucket(Bucket=storage.bucket)

    recorder = RecordingS3Storage(storage)
    try:
        yield recorder
    finally:
        await recorder.cleanup()


@pytest.fixture
async def submission_client(
    client,
    recording_minio_storage: RecordingS3Storage,
) -> AsyncIterator:
    """Override storage with a real MinIO adapter for submission endpoint tests."""
    previous = app.dependency_overrides.get(get_object_storage)
    app.dependency_overrides[get_object_storage] = lambda: recording_minio_storage
    try:
        yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_object_storage, None)
        else:
            app.dependency_overrides[get_object_storage] = previous
