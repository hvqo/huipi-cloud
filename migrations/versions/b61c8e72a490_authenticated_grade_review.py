"""可信候选评分审核人；保留历史CLI身份和原有不可变触发器。"""

import sqlalchemy as sa
from alembic import op

revision = "b61c8e72a490"
down_revision = "9d2a17c4e6b0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("grade_review_revisions", sa.Column("reviewer_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_grade_review_reviewer_user",
        "grade_review_revisions",
        "users",
        ["reviewer_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_grade_review_reviewer_user", "grade_review_revisions", ["reviewer_user_id"])
    op.create_check_constraint(
        "ck_grade_review_authenticated_reviewer",
        "grade_review_revisions",
        "reviewer_user_id IS NULL OR reviewer_ref = 'teacher_' || "
        "replace(reviewer_user_id::text, '-', '')",
    )


def downgrade():
    op.drop_constraint("ck_grade_review_authenticated_reviewer", "grade_review_revisions")
    op.drop_index("ix_grade_review_reviewer_user", table_name="grade_review_revisions")
    op.drop_constraint(
        "fk_grade_review_reviewer_user", "grade_review_revisions", type_="foreignkey"
    )
    op.drop_column("grade_review_revisions", "reviewer_user_id")
