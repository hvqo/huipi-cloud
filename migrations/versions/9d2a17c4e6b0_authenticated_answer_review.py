"""可信作答审核人外键与数据库追加写保护；不回填历史身份。"""

import sqlalchemy as sa
from alembic import op

revision = "9d2a17c4e6b0"
down_revision = "6a9d8c7e2f10"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "answer_review_decisions", sa.Column("reviewer_user_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_answer_review_reviewer_user",
        "answer_review_decisions",
        "users",
        ["reviewer_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_answer_review_reviewer_user", "answer_review_decisions", ["reviewer_user_id"]
    )
    op.create_check_constraint(
        "ck_answer_review_authenticated_reviewer",
        "answer_review_decisions",
        "reviewer_user_id IS NULL OR reviewer_ref = 'teacher_' || "
        "replace(reviewer_user_id::text, '-', '')",
    )
    op.execute("""
        CREATE FUNCTION reject_answer_review_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'answer_review_append_only' USING ERRCODE = '23514';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_answer_review_immutable BEFORE UPDATE OR DELETE
        ON answer_review_decisions FOR EACH ROW EXECUTE FUNCTION reject_answer_review_mutation()
    """)


def downgrade():
    op.execute("DROP TRIGGER trg_answer_review_immutable ON answer_review_decisions")
    op.execute("DROP FUNCTION reject_answer_review_mutation()")
    op.drop_constraint("ck_answer_review_authenticated_reviewer", "answer_review_decisions")
    op.drop_index("ix_answer_review_reviewer_user", table_name="answer_review_decisions")
    op.drop_constraint(
        "fk_answer_review_reviewer_user", "answer_review_decisions", type_="foreignkey"
    )
    op.drop_column("answer_review_decisions", "reviewer_user_id")
