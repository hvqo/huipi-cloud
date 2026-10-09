"""Light PostgreSQL index for immutable Canonical Document objects."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base, utc_now


class CanonicalArtifact(Base):
    """Successful canonical object index or a safe deterministic failure marker."""

    __tablename__ = "canonical_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "parsed_artifact_id",
            "normalizer_version",
            name="uq_canonical_artifacts_source_normalizer",
        ),
        CheckConstraint(
            "status IN ('available', 'failed')",
            name="ck_canonical_artifacts_status",
        ),
        CheckConstraint(
            "(status = 'available' AND canonical_object_key IS NOT NULL "
            "AND length(trim(canonical_object_key)) > 0 AND length(canonical_sha256) = 64 "
            "AND canonical_size_bytes >= 0 AND page_count > 0 AND block_count >= 0 "
            "AND failure_code IS NULL) OR "
            "(status = 'failed' AND canonical_object_key IS NULL "
            "AND canonical_sha256 IS NULL AND canonical_size_bytes IS NULL "
            "AND page_count IS NULL AND block_count IS NULL "
            "AND failure_code IS NOT NULL)",
            name="ck_canonical_artifacts_result_shape",
        ),
        CheckConstraint(
            "length(trim(bucket)) > 0 AND length(trim(normalizer_version)) > 0",
            name="ck_canonical_artifacts_storage_and_version",
        ),
        Index("ix_canonical_artifacts_submission_created_at", "submission_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    parsed_artifact_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("parsed_artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    submission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
    )
    bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    canonical_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    canonical_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    block_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_parser: Mapped[str] = mapped_column(String(32), nullable=False)
    source_parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
