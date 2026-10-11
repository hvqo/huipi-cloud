"""Add immutable visual evidence proposal indexes.

Revision ID: 5f43a97b2c10
Revises: 3c9a6f12d8e4
Create Date: 2026-10-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5f43a97b2c10"
down_revision: Union[str, Sequence[str], None] = "3c9a6f12d8e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "visual_evidence_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("question_number", sa.Integer(), nullable=False),
        sa.Column("canonical_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_sha256", sa.String(length=64), nullable=False),
        sa.Column("alignment_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("alignment_sha256", sa.String(length=64), nullable=False),
        sa.Column("original_file_sha256", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("preprocessing_version", sa.String(length=32), nullable=False),
        sa.Column("model_input_digest", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column("proposal_bucket", sa.String(length=63), nullable=False),
        sa.Column("proposal_object_key", sa.String(length=1024), nullable=False),
        sa.Column("proposal_sha256", sa.String(length=64), nullable=False),
        sa.Column("proposal_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "outcome IN ('candidate_response_present', 'candidate_prompt_only', 'uncertain')",
            name="ck_visual_evidence_outcome",
        ),
        sa.CheckConstraint("question_number > 0", name="ck_visual_evidence_question_number"),
        sa.CheckConstraint(
            "length(canonical_sha256) = 64 AND length(alignment_sha256) = 64 "
            "AND length(original_file_sha256) = 64 AND length(model_input_digest) = 64 "
            "AND length(proposal_sha256) = 64",
            name="ck_visual_evidence_digests",
        ),
        sa.CheckConstraint(
            "proposal_size_bytes > 0 AND length(trim(proposal_bucket)) > 0 "
            "AND length(trim(proposal_object_key)) > 0 AND length(trim(model_id)) > 0",
            name="ck_visual_evidence_storage_and_model",
        ),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["canonical_artifact_id"], ["canonical_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["alignment_artifact_id"], ["answer_alignment_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "submission_id",
            "question_id",
            "request_id",
            name="uq_visual_evidence_submission_question_request",
        ),
        sa.UniqueConstraint(
            "proposal_bucket",
            "proposal_object_key",
            name="uq_visual_evidence_bucket_object_key",
        ),
    )
    op.create_index(
        "ix_visual_evidence_submission_created_at",
        "visual_evidence_artifacts",
        ["submission_id", "created_at"],
    )
    op.create_index(
        "ix_visual_evidence_question_created_at",
        "visual_evidence_artifacts",
        ["question_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_visual_evidence_question_created_at", table_name="visual_evidence_artifacts")
    op.drop_index(
        "ix_visual_evidence_submission_created_at",
        table_name="visual_evidence_artifacts",
    )
    op.drop_table("visual_evidence_artifacts")
