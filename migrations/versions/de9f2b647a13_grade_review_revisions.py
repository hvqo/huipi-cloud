"""Add append-only grading proposal review revisions.

Revision ID: de9f2b647a13
Revises: 2b4e91c6d0a7
Create Date: 2026-10-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "de9f2b647a13"
down_revision: Union[str, Sequence[str], None] = "2b4e91c6d0a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "grade_review_revisions",
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("grade_input_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_sha256", sa.String(length=64), nullable=False),
        sa.Column("grade_input_sha256", sa.String(length=64), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("review_schema_version", sa.String(length=16), nullable=False),
        sa.Column("review_revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_review_id", sa.Uuid(), nullable=True),
        sa.Column("reviewer_ref", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("review_status", sa.String(length=24), nullable=False),
        sa.Column(
            "criterion_reviews",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "review_reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.Column("proposed_total_score", sa.Numeric(8, 2), nullable=True),
        sa.Column("reviewed_total_score", sa.Numeric(8, 2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "review_status IN ('accepted', 'revised', 'rejected', 'regrading_required')",
            name="ck_grade_review_status",
        ),
        sa.CheckConstraint("review_revision > 0", name="ck_grade_review_revision_positive"),
        sa.CheckConstraint(
            "review_schema_version = '1.0' AND length(trim(reviewer_ref)) > 0 "
            "AND length(proposal_sha256) = 64 AND length(grade_input_sha256) = 64 "
            "AND length(request_sha256) = 64",
            name="ck_grade_review_metadata",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(criterion_reviews) = 'array' "
            "AND jsonb_typeof(review_reason_codes) = 'array'",
            name="ck_grade_review_json_arrays",
        ),
        sa.CheckConstraint(
            "((review_status IN ('accepted', 'revised')) "
            "AND jsonb_array_length(criterion_reviews) > 0 "
            "AND proposed_total_score IS NOT NULL AND reviewed_total_score IS NOT NULL) "
            "OR ((review_status IN ('rejected', 'regrading_required')) "
            "AND jsonb_array_length(criterion_reviews) = 0 "
            "AND reviewed_total_score IS NULL)",
            name="ck_grade_review_score_shape",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["grade_proposal_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["grade_input_artifact_id"], ["grade_input_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["supersedes_review_id"],
            ["grade_review_revisions.review_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("review_id"),
        sa.UniqueConstraint("proposal_id", "review_revision", name="uq_grade_review_revision"),
        sa.UniqueConstraint("proposal_id", "request_id", name="uq_grade_review_request"),
        sa.UniqueConstraint("supersedes_review_id", name="uq_grade_review_successor"),
    )
    op.create_index(
        "ix_grade_review_proposal_created",
        "grade_review_revisions",
        ["proposal_id", "created_at"],
    )
    op.execute(
        """
        CREATE FUNCTION reject_grade_review_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'grade review revisions are append-only'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_grade_review_revisions_immutable
        BEFORE UPDATE OR DELETE ON grade_review_revisions
        FOR EACH ROW EXECUTE FUNCTION reject_grade_review_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_grade_review_revisions_immutable ON grade_review_revisions"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_grade_review_mutation()")
    op.drop_index("ix_grade_review_proposal_created", table_name="grade_review_revisions")
    op.drop_table("grade_review_revisions")
