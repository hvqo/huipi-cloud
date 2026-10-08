"""新增解析产物索引

Revision ID: 339018f72219
Revises: 8b2c0f7d1a43
Create Date: 2026-10-09 01:20:13.118176

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "339018f72219"
down_revision: Union[str, Sequence[str], None] = "8b2c0f7d1a43"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "parsed_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("parsing_task_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("bucket", sa.String(length=63), nullable=False),
        sa.Column("original_sha256", sa.String(length=64), nullable=False),
        sa.Column("parser_name", sa.String(length=32), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("tier", sa.String(length=16), nullable=False),
        sa.Column("schema_name", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("asset_count", sa.Integer(), nullable=False),
        sa.Column("archive_key", sa.String(length=1024), nullable=False),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("archive_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("markdown_key", sa.String(length=1024), nullable=False),
        sa.Column("markdown_sha256", sa.String(length=64), nullable=False),
        sa.Column("markdown_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("middle_json_key", sa.String(length=1024), nullable=False),
        sa.Column("middle_json_sha256", sa.String(length=64), nullable=False),
        sa.Column("middle_json_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("structured_content_key", sa.String(length=1024), nullable=False),
        sa.Column("structured_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("structured_content_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("assets_manifest_key", sa.String(length=1024), nullable=False),
        sa.Column("assets_manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("assets_manifest_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "schema_name = 'docvortex.middle' AND schema_version = '2.0'",
            name="ck_parsed_artifacts_middle_schema",
        ),
        sa.CheckConstraint(
            "archive_size_bytes >= 0 AND markdown_size_bytes >= 0 "
            "AND middle_json_size_bytes >= 0 AND structured_content_size_bytes >= 0 "
            "AND assets_manifest_size_bytes >= 0",
            name="ck_parsed_artifacts_sizes",
        ),
        sa.CheckConstraint(
            "asset_count >= 0", name="ck_parsed_artifacts_asset_count"
        ),
        sa.CheckConstraint(
            "length(original_sha256) = 64 AND length(archive_sha256) = 64 "
            "AND length(markdown_sha256) = 64 AND length(middle_json_sha256) = 64 "
            "AND length(structured_content_sha256) = 64 "
            "AND length(assets_manifest_sha256) = 64",
            name="ck_parsed_artifacts_sha256_lengths",
        ),
        sa.CheckConstraint(
            "page_count > 0", name="ck_parsed_artifacts_page_count"
        ),
        sa.ForeignKeyConstraint(
            ["parsing_task_id"], ["parsing_tasks.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parsing_task_id"),
        sa.UniqueConstraint("submission_id"),
    )
    op.create_index(
        "ix_parsed_artifacts_created_at",
        "parsed_artifacts",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_parsed_artifacts_created_at", table_name="parsed_artifacts")
    op.drop_table("parsed_artifacts")
