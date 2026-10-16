"""教师可信身份与作业成员授权；历史作业不自动授权。"""

import sqlalchemy as sa
from alembic import op

revision = "6a9d8c7e2f10"
down_revision = "0fd471e9f785"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),
        sa.CheckConstraint("length(trim(issuer)) > 0", name="ck_users_issuer_not_blank"),
        sa.CheckConstraint("length(trim(subject)) > 0", name="ck_users_subject_not_blank"),
    )
    op.create_table(
        "assignment_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("can_write", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("assignment_id", "user_id", name="uq_assignment_memberships_user"),
    )
    op.create_index(
        "ix_assignment_memberships_user_assignment",
        "assignment_memberships",
        ["user_id", "assignment_id"],
    )


def downgrade():
    op.drop_index("ix_assignment_memberships_user_assignment", table_name="assignment_memberships")
    op.drop_table("assignment_memberships")
    op.drop_table("users")
