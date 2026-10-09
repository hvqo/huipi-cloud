"""Add immutable Answer Alignment result index.

Revision ID: a4b76d218c11
Revises: f10c92b373a6
Create Date: 2026-10-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a4b76d218c11"
down_revision: Union[str, Sequence[str], None] = "f10c92b373a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "answer_alignment_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_sha256", sa.String(length=64), nullable=False),
        sa.Column("aligner_version", sa.String(length=32), nullable=False),
        sa.Column("assignment_questions_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("bucket", sa.String(length=63), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("alignment_sha256", sa.String(length=64), nullable=False),
        sa.Column("alignment_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("question_count", sa.Integer(), nullable=False),
        sa.Column("aligned_count", sa.Integer(), nullable=False),
        sa.Column("review_required_count", sa.Integer(), nullable=False),
        sa.Column("unmatched_count", sa.Integer(), nullable=False),
        sa.Column("not_observed_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('complete', 'review_required')",
            name="ck_answer_alignment_status",
        ),
        sa.CheckConstraint(
            "length(canonical_sha256) = 64 AND length(alignment_sha256) = 64 "
            "AND length(assignment_questions_digest) = 64",
            name="ck_answer_alignment_digests",
        ),
        sa.CheckConstraint(
            "length(trim(bucket)) > 0 AND length(trim(object_key)) > 0 "
            "AND length(trim(aligner_version)) > 0",
            name="ck_answer_alignment_storage_and_version",
        ),
        sa.CheckConstraint(
            "alignment_size_bytes > 0 AND question_count >= 0 AND aligned_count >= 0 "
            "AND review_required_count >= 0 AND unmatched_count >= 0 "
            "AND not_observed_count >= 0",
            name="ck_answer_alignment_counts",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["assignments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["canonical_artifact_id"], ["canonical_artifacts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "canonical_artifact_id",
            "aligner_version",
            "assignment_questions_digest",
            name="uq_answer_alignments_canonical_aligner_questions",
        ),
    )
    op.create_index(
        "ix_answer_alignment_submission_created_at",
        "answer_alignment_artifacts",
        ["submission_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_answer_alignment_submission_created_at",
        table_name="answer_alignment_artifacts",
    )
    op.drop_table("answer_alignment_artifacts")
