"""Add local credentials and revocable sessions without rewriting historical users."""

import sqlalchemy as sa
from alembic import op

revision = "c21a48d3e023"
down_revision = "b20b37c2e022"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "local_credentials",
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True
        ),
        sa.Column("login_name", sa.String(254), nullable=False),
        sa.Column("display_name", sa.String(100)),
        sa.Column("recovery_email", sa.String(254)),
        sa.Column("recovery_verified_at", sa.DateTime(timezone=True)),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("must_change_password", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("login_name", name="uq_local_credentials_login_name"),
        sa.UniqueConstraint("recovery_email", name="uq_local_credentials_recovery_email"),
        sa.CheckConstraint("length(login_name) BETWEEN 3 AND 254", name="ck_local_login_length"),
        sa.CheckConstraint(
            "(recovery_email IS NULL AND recovery_verified_at IS NULL) OR "
            "(recovery_email IS NOT NULL AND recovery_verified_at IS NOT NULL)",
            name="ck_local_recovery_email_verified",
        ),
    )
    op.create_table(
        "local_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_local_session_token_hash"),
        sa.CheckConstraint("length(csrf_hash) = 64", name="ck_local_session_csrf_hash"),
    )
    op.create_index("ix_local_sessions_user_active", "local_sessions", ["user_id", "expires_at"])
    op.create_table(
        "local_auth_throttles",
        sa.Column("key_hash", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "local_password_resets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("target_email", sa.String(254)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("token_hash", name="uq_local_password_reset_token"),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_local_password_reset_token_hash"),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'reset_password') AND target_email IS NOT NULL",
            name="ck_local_password_reset_purpose",
        ),
    )
    op.create_index(
        "ix_local_password_resets_user", "local_password_resets", ["user_id", "created_at"]
    )
    op.create_table(
        "local_auth_audit",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_user_id", sa.Uuid()),
        sa.Column("target_user_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("request_id", sa.Uuid()),
        sa.Column("payload_sha256", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "actor_user_id", "request_id", name="uq_local_auth_audit_actor_request"
        ),
    )
    op.create_index(
        "ix_local_auth_audit_target_created", "local_auth_audit", ["target_user_id", "created_at"]
    )
    op.execute("""
        CREATE FUNCTION reject_local_auth_audit_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'local_auth_audit_immutable' USING ERRCODE = '23514';
        END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER local_auth_audit_immutable BEFORE UPDATE OR DELETE ON local_auth_audit
        FOR EACH ROW EXECUTE FUNCTION reject_local_auth_audit_mutation()
    """)


def downgrade():
    # Never silently discard locally provisioned accounts or their security history.
    op.execute(
        "LOCK TABLE local_credentials, local_sessions, local_password_resets, "
        "local_auth_audit IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM local_credentials) OR EXISTS (SELECT 1 FROM local_auth_audit)
             OR EXISTS (SELECT 1 FROM local_sessions)
             OR EXISTS (SELECT 1 FROM local_password_resets)
          THEN RAISE EXCEPTION 'local_auth_facts_prevent_downgrade'; END IF;
        END $$
    """)
    for table in (
        "local_auth_audit",
        "local_password_resets",
        "local_auth_throttles",
        "local_sessions",
        "local_credentials",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS reject_local_auth_audit_mutation()")
