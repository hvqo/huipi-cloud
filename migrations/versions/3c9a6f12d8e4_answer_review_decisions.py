"""Add append-only answer review decisions.

Revision ID: 3c9a6f12d8e4
Revises: a4b76d218c11
Create Date: 2026-10-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3c9a6f12d8e4"
down_revision: Union[str, Sequence[str], None] = "a4b76d218c11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "answer_review_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("question_number", sa.Integer(), nullable=False),
        sa.Column("alignment_status", sa.String(length=24), nullable=False),
        sa.Column("alignment_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("alignment_sha256", sa.String(length=64), nullable=False),
        sa.Column("aligner_version", sa.String(length=32), nullable=False),
        sa.Column("assignment_questions_digest", sa.String(length=64), nullable=False),
        sa.Column("canonical_document_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_sha256", sa.String(length=64), nullable=False),
        sa.Column("review_schema_version", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column(
            "response_regions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "excluded_prompt_regions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "uncertain_regions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "reason_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("reviewer_ref", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_decision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('response_present', 'response_absent', 'uncertain')",
            name="ck_answer_review_decision_value",
        ),
        sa.CheckConstraint(
            "alignment_status IN ('aligned', 'review_required', 'not_observed')",
            name="ck_answer_review_alignment_status",
        ),
        sa.CheckConstraint("question_number > 0 AND revision > 0", name="ck_answer_review_numbers"),
        sa.CheckConstraint(
            "length(alignment_sha256) = 64 AND length(canonical_sha256) = 64 "
            "AND length(assignment_questions_digest) = 64 AND length(request_sha256) = 64",
            name="ck_answer_review_digests",
        ),
        sa.CheckConstraint(
            "length(trim(aligner_version)) > 0 AND review_schema_version = '1.0' "
            "AND length(trim(reviewer_ref)) > 0",
            name="ck_answer_review_nonblank_metadata",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(response_regions) = 'array' "
            "AND jsonb_typeof(excluded_prompt_regions) = 'array' "
            "AND jsonb_typeof(uncertain_regions) = 'array' "
            "AND jsonb_typeof(reason_codes) = 'array'",
            name="ck_answer_review_json_arrays",
        ),
        sa.CheckConstraint(
            "(decision = 'response_present' AND jsonb_array_length(response_regions) > 0 "
            "AND jsonb_array_length(uncertain_regions) = 0) OR "
            "(decision = 'response_absent' AND jsonb_array_length(response_regions) = 0 "
            "AND jsonb_array_length(uncertain_regions) = 0 "
            "AND jsonb_array_length(excluded_prompt_regions) > 0) OR "
            "(decision = 'uncertain' AND jsonb_array_length(response_regions) = 0 "
            "AND (jsonb_array_length(uncertain_regions) > 0 "
            "OR reason_codes @> '[\"source_quality_insufficient\"]'::jsonb "
            "OR reason_codes @> '[\"response_not_linked_to_question\"]'::jsonb))",
            name="ck_answer_review_decision_evidence",
        ),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["alignment_artifact_id"],
            ["answer_alignment_artifacts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_document_id"], ["canonical_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_id"],
            ["answer_review_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "submission_id",
            "question_id",
            "revision",
            name="uq_answer_review_submission_question_revision",
        ),
        sa.UniqueConstraint(
            "submission_id",
            "question_id",
            "request_id",
            name="uq_answer_review_submission_question_request",
        ),
        sa.UniqueConstraint("supersedes_decision_id", name="uq_answer_review_successor"),
    )


def downgrade() -> None:
    op.drop_table("answer_review_decisions")
