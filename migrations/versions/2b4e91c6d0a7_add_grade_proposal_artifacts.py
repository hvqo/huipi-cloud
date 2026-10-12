"""Add immutable rubric grading proposal indexes.

Revision ID: 2b4e91c6d0a7
Revises: 7d25c4a18f03
Create Date: 2026-10-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2b4e91c6d0a7"
down_revision: Union[str, None] = "7d25c4a18f03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "grade_proposal_artifacts",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("request_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("grade_input_artifact_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("grade_input_sha256", sa.String(length=64), nullable=False),
        sa.Column("submission_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("question_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("generation_config_digest", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("grading_status", sa.String(length=32), nullable=False),
        sa.Column("bucket", sa.String(length=63), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("object_sha256", sa.String(length=64), nullable=False),
        sa.Column("object_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=True),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=True),
        sa.Column("model_elapsed_ms", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(grade_input_sha256) = 64 AND length(request_fingerprint) = 64 "
            "AND length(generation_config_digest) = 64 AND length(object_sha256) = 64",
            name="ck_grade_proposals_digests",
        ),
        sa.CheckConstraint(
            "grading_status IN ('completed_candidate', 'grading_abstained') "
            "AND length(trim(provider)) > 0 AND length(trim(model_id)) > 0 "
            "AND length(trim(prompt_version)) > 0 AND length(trim(bucket)) > 0 "
            "AND length(trim(object_key)) > 0 AND object_size_bytes > 0 "
            "AND model_elapsed_ms >= 0",
            name="ck_grade_proposals_metadata",
        ),
        sa.CheckConstraint(
            "(prompt_tokens IS NULL OR prompt_tokens >= 0) "
            "AND (completion_tokens IS NULL OR completion_tokens >= 0)",
            name="ck_grade_proposals_usage",
        ),
        sa.ForeignKeyConstraint(
            ["grade_input_artifact_id"],
            ["grade_input_artifacts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bucket", "object_key", name="uq_grade_proposals_bucket_object_key"
        ),
        sa.UniqueConstraint("request_id", name="uq_grade_proposals_request_id"),
    )
    op.create_index(
        "ix_grade_proposals_input_created",
        "grade_proposal_artifacts",
        ["grade_input_artifact_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_grade_proposals_input_created", table_name="grade_proposal_artifacts")
    op.drop_table("grade_proposal_artifacts")
