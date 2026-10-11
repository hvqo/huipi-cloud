"""Add immutable GradeInput artifact indexes.

Revision ID: 7d25c4a18f03
Revises: 5f43a97b2c10
Create Date: 2026-10-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7d25c4a18f03"
down_revision: Union[str, Sequence[str], None] = "5f43a97b2c10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "grade_input_artifacts",
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
        sa.Column("assignment_questions_digest", sa.String(length=64), nullable=False),
        sa.Column("review_decision_id", sa.Uuid(), nullable=False),
        sa.Column("review_revision", sa.Integer(), nullable=False),
        sa.Column("review_request_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_file_sha256", sa.String(length=64), nullable=False),
        sa.Column("answer_key_id", sa.Uuid(), nullable=False),
        sa.Column("answer_key_sha256", sa.String(length=64), nullable=False),
        sa.Column("rubric_sha256", sa.String(length=64), nullable=False),
        sa.Column("input_sha256", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("bucket", sa.String(length=63), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("object_sha256", sa.String(length=64), nullable=False),
        sa.Column("object_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "question_number > 0 AND review_revision > 0",
            name="ck_grade_inputs_numbers",
        ),
        sa.CheckConstraint(
            "length(canonical_sha256) = 64 AND length(alignment_sha256) = 64 "
            "AND length(assignment_questions_digest) = 64 "
            "AND length(review_request_sha256) = 64 AND length(source_file_sha256) = 64 "
            "AND length(answer_key_sha256) = 64 AND length(rubric_sha256) = 64 "
            "AND length(input_sha256) = 64 AND length(object_sha256) = 64",
            name="ck_grade_inputs_digests",
        ),
        sa.CheckConstraint(
            "length(trim(schema_version)) > 0 AND length(trim(bucket)) > 0 "
            "AND length(trim(object_key)) > 0 AND object_size_bytes > 0",
            name="ck_grade_inputs_storage_metadata",
        ),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["canonical_artifact_id"], ["canonical_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["alignment_artifact_id"],
            ["answer_alignment_artifacts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["review_decision_id"], ["answer_review_decisions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["answer_key_id"], ["answer_keys.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "submission_id",
            "question_id",
            "request_id",
            name="uq_grade_inputs_submission_question_request",
        ),
        sa.UniqueConstraint("bucket", "object_key", name="uq_grade_inputs_bucket_object_key"),
    )
    op.create_index(
        "ix_grade_inputs_submission_created",
        "grade_input_artifacts",
        ["submission_id", "created_at"],
    )
    op.create_index(
        "ix_grade_inputs_question_created",
        "grade_input_artifacts",
        ["question_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_grade_inputs_question_created", table_name="grade_input_artifacts")
    op.drop_index("ix_grade_inputs_submission_created", table_name="grade_input_artifacts")
    op.drop_table("grade_input_artifacts")
