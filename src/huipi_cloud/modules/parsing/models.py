"""Database index for immutable MinerU result bundles in private object storage."""

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
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from huipi_cloud.infrastructure.database.base import Base, utc_now


class ParsedArtifact(Base):
    """One successful parser run, indexed only after its lease-fenced transaction."""

    __tablename__ = "parsed_artifacts"
    __table_args__ = (
        CheckConstraint("page_count > 0", name="ck_parsed_artifacts_page_count"),
        CheckConstraint("asset_count >= 0", name="ck_parsed_artifacts_asset_count"),
        CheckConstraint(
            "archive_size_bytes >= 0 AND markdown_size_bytes >= 0 "
            "AND middle_json_size_bytes >= 0 AND structured_content_size_bytes >= 0 "
            "AND assets_manifest_size_bytes >= 0",
            name="ck_parsed_artifacts_sizes",
        ),
        CheckConstraint(
            "length(original_sha256) = 64 AND length(archive_sha256) = 64 "
            "AND length(markdown_sha256) = 64 AND length(middle_json_sha256) = 64 "
            "AND length(structured_content_sha256) = 64 "
            "AND length(assets_manifest_sha256) = 64",
            name="ck_parsed_artifacts_sha256_lengths",
        ),
        CheckConstraint(
            "schema_name = 'docvortex.middle' AND schema_version = '2.0'",
            name="ck_parsed_artifacts_middle_schema",
        ),
        Index("ix_parsed_artifacts_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    parsing_task_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("parsing_tasks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    submission_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    bucket: Mapped[str] = mapped_column(String(63), nullable=False)
    original_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_name: Mapped[str] = mapped_column(String(32), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_name: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_count: Mapped[int] = mapped_column(Integer, nullable=False)
    archive_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    archive_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    archive_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    markdown_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    markdown_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    markdown_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    middle_json_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    middle_json_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    middle_json_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    structured_content_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    structured_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    structured_content_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    assets_manifest_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    assets_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    assets_manifest_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=func.now(),
    )
