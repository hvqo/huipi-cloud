"""Async SQLAlchemy engine and request-scoped session dependency."""

from collections.abc import AsyncIterator

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from huipi_cloud.core.config import settings

engine: AsyncEngine | None = (
    create_async_engine(settings.database_url, pool_pre_ping=True)
    if settings.database_url
    else None
)
session_factory = (
    async_sessionmaker(engine, expire_on_commit=False)
    if engine is not None
    else None
)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield one database session for a request."""
    factory = session_factory
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="数据库尚未配置，请设置 DATABASE_URL",
        )
    async with factory() as session:
        yield session


async def dispose_database_engine() -> None:
    """Close the process-wide connection pool during application shutdown."""
    if engine is not None:
        await engine.dispose()
