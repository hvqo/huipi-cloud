"""不可变管理操作审计；不改变既有教师、作业和授权数据。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c72d9e14a510"
down_revision = "b61c8e72a490"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "admin_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=True),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("actor_user_id", "request_id", name="uq_admin_operations_request"),
        sa.CheckConstraint("length(payload_sha256) = 64", name="ck_admin_operations_digest"),
        sa.CheckConstraint("jsonb_typeof(state) = 'object'", name="ck_admin_operations_state"),
        sa.CheckConstraint(
            "action IN ('teacher_registered', 'teacher_activation_changed', "
            "'assignment_membership_granted', 'assignment_membership_revoked')",
            name="ck_admin_operations_action",
        ),
    )
    op.create_index(
        "ix_admin_operations_target_created", "admin_operations", ["target_user_id", "created_at"]
    )
    op.create_index(
        "ix_admin_operations_assignment_created",
        "admin_operations",
        ["assignment_id", "created_at"],
    )
    op.execute("""
        CREATE FUNCTION prevent_admin_operation_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'admin operations are immutable' USING ERRCODE = '23514';
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER immutable_admin_operations BEFORE UPDATE OR DELETE ON admin_operations
        FOR EACH ROW EXECUTE FUNCTION prevent_admin_operation_mutation();
    """)


def downgrade():
    op.drop_table("admin_operations")
    op.execute("DROP FUNCTION prevent_admin_operation_mutation()")
