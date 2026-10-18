"""Shared SQLAlchemy declarative base and UTC timestamp helper."""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base for SQLAlchemy ORM models."""


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


# Import installs a lazy ORM policy without importing models during Base creation.
from huipi_cloud.modules.resource_security import visibility as _visibility  # noqa: E402,F401
