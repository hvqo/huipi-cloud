"""B-1.1a: immutable resource class and bounded security capability foundation."""

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e21a9b4c110a"
down_revision = "c72d9e14a510"
branch_labels = None
depends_on = None


def uid(name, target=None, *, primary_key=False, nullable=False):
    args = [sa.ForeignKey(target)] if target else []
    return sa.Column(name, sa.Uuid(), *args, primary_key=primary_key, nullable=nullable)


def upgrade():
    op.add_column(
        "assignments",
        sa.Column(
            "security_classification",
            sa.String(24),
            nullable=False,
            server_default="legacy_homework",
        ),
    )
    op.create_check_constraint(
        "ck_assignments_security_classification",
        "assignments",
        "security_classification IN ('legacy_homework','exam_protected','unclassified')",
    )
    op.execute("""
      CREATE FUNCTION protect_assignment_classification() RETURNS trigger AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN
          IF OLD.security_classification <> 'legacy_homework' THEN
            RAISE EXCEPTION 'protected_resource_delete_forbidden' USING ERRCODE='23514';
          END IF;
          RETURN OLD;
        END IF;
        IF NEW.security_classification IS DISTINCT FROM OLD.security_classification THEN
          RAISE EXCEPTION 'resource_classification_immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
      CREATE TRIGGER immutable_assignment_classification BEFORE UPDATE OR DELETE ON assignments
      FOR EACH ROW EXECUTE FUNCTION protect_assignment_classification();
    """)
    op.create_table(
        "security_installations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.CheckConstraint("id = 1", name="ck_security_single_installation"),
    )
    op.create_table(
        "permission_definitions",
        sa.Column("code", sa.String(80), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("scope_kinds", postgresql.ARRAY(sa.String(24)), nullable=False),
    )
    op.create_table(
        "security_scopes",
        uid("id", primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("installation_id", sa.Integer(), sa.ForeignKey("security_installations.id")),
        uid("assignment_id", "assignments.id", nullable=True),
        sa.CheckConstraint(
            "(kind = 'installation' AND installation_id = 1 AND assignment_id IS NULL) OR "
            "(kind = 'assignment' AND installation_id IS NULL AND assignment_id IS NOT NULL)",
            name="ck_security_scope_shape",
        ),
        sa.UniqueConstraint("assignment_id", name="uq_security_scope_assignment"),
        sa.UniqueConstraint("installation_id", name="uq_security_scope_installation"),
    )
    op.create_table(
        "security_role_versions",
        uid("id", primary_key=True),
        sa.Column("role_code", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("role_code", "revision", name="uq_security_role_version"),
        sa.CheckConstraint("revision > 0", name="ck_security_role_revision"),
    )
    op.create_table(
        "security_role_permissions",
        uid("role_version_id", "security_role_versions.id", primary_key=True),
        sa.Column(
            "permission_code",
            sa.String(80),
            sa.ForeignKey("permission_definitions.code"),
            primary_key=True,
        ),
    )
    op.create_table(
        "security_grants",
        uid("id", primary_key=True),
        uid("user_id", "users.id"),
        uid("role_version_id", "security_role_versions.id"),
        uid("scope_id", "security_scopes.id"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_security_grants_user_scope", "security_grants", ["user_id", "scope_id"])
    op.create_table(
        "security_audit_events",
        uid("id", primary_key=True),
        uid("actor_user_id"),
        uid("request_id"),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("actor_user_id", "request_id", name="uq_security_audit_request"),
        sa.CheckConstraint("length(payload_sha256) = 64", name="ck_security_audit_sha"),
        sa.CheckConstraint(
            "action IN ('bootstrap','grant','revoke','protected_create')",
            name="ck_security_audit_action",
        ),
    )
    bind = op.get_bind()
    bind.execute(sa.text("INSERT INTO security_installations VALUES (1)"))
    permissions = {
        "security.grants.manage": (True, ["installation"]),
        "security.protected.create": (True, ["installation"]),
        "security.classification.inspect": (True, ["installation", "assignment"]),
        "exam.session.read": (False, []),
    }
    table = sa.table(
        "permission_definitions",
        sa.column("code"),
        sa.column("enabled"),
        sa.column("scope_kinds", postgresql.ARRAY(sa.String(24))),
    )
    bind.execute(
        table.insert(),
        [dict(code=c, enabled=e, scope_kinds=k) for c, (e, k) in permissions.items()],
    )
    roles = [
        ("11111111-b11a-4000-8000-000000000002", "security_operator", list(permissions)[:3]),
        (
            "11111111-b11a-4000-8000-000000000003",
            "protected_creator",
            ["security.protected.create"],
        ),
        (
            "11111111-b11a-4000-8000-000000000004",
            "classification_auditor",
            ["security.classification.inspect"],
        ),
    ]
    for role_id, code, codes in roles:
        bind.execute(
            sa.text("INSERT INTO security_role_versions VALUES (:id,:code,1,:sealed)"),
            dict(id=UUID(role_id), code=code, sealed=datetime.now(UTC)),
        )
        for permission in codes:
            bind.execute(
                sa.text("INSERT INTO security_role_permissions VALUES (:id,:permission)"),
                dict(id=UUID(role_id), permission=permission),
            )
    op.execute("""
      CREATE FUNCTION guard_security_immutable() RETURNS trigger AS $$
      BEGIN RAISE EXCEPTION 'security_record_immutable' USING ERRCODE='23514'; END;
      $$ LANGUAGE plpgsql
    """)
    op.execute("""
      CREATE TRIGGER immutable_security_audit BEFORE UPDATE OR DELETE ON security_audit_events
      FOR EACH ROW EXECUTE FUNCTION guard_security_immutable()
    """)
    op.execute("""
      CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON security_scopes
      FOR EACH ROW EXECUTE FUNCTION guard_security_immutable()
    """)
    op.execute("""
      CREATE FUNCTION guard_security_grant() RETURNS trigger AS $$
      BEGIN
        IF NEW.id IS DISTINCT FROM OLD.id OR NEW.user_id IS DISTINCT FROM OLD.user_id
          OR NEW.role_version_id IS DISTINCT FROM OLD.role_version_id
          OR NEW.scope_id IS DISTINCT FROM OLD.scope_id
          OR NEW.created_at IS DISTINCT FROM OLD.created_at
          OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
        THEN RAISE EXCEPTION 'security_grant_identity_immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
      CREATE TRIGGER immutable_security_grant BEFORE UPDATE ON security_grants
      FOR EACH ROW EXECUTE FUNCTION guard_security_grant()
    """)
    op.execute("""
      CREATE FUNCTION guard_security_role() RETURNS trigger AS $$
      BEGIN
        IF TG_OP = 'UPDATE' AND OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL
          AND NEW.id = OLD.id AND NEW.role_code = OLD.role_code AND NEW.revision = OLD.revision
        THEN RETURN NEW; END IF;
        RAISE EXCEPTION 'security_role_immutable' USING ERRCODE='23514';
      END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
      CREATE TRIGGER immutable_security_role BEFORE UPDATE OR DELETE ON security_role_versions
      FOR EACH ROW EXECUTE FUNCTION guard_security_role()
    """)
    op.execute("""
      CREATE FUNCTION guard_security_role_permission() RETURNS trigger AS $$
      DECLARE sealed timestamptz;
      BEGIN
        IF TG_OP <> 'INSERT' THEN
          SELECT sealed_at INTO sealed FROM security_role_versions
            WHERE id=OLD.role_version_id FOR SHARE;
          IF sealed IS NOT NULL THEN
            RAISE EXCEPTION 'security_role_sealed' USING ERRCODE='23514';
          END IF;
        END IF;
        IF TG_OP <> 'DELETE' THEN
          SELECT sealed_at INTO sealed FROM security_role_versions
            WHERE id=NEW.role_version_id FOR SHARE;
          IF sealed IS NOT NULL THEN
            RAISE EXCEPTION 'security_role_sealed' USING ERRCODE='23514';
          END IF;
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
      END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
      CREATE TRIGGER immutable_security_role_permissions BEFORE INSERT OR UPDATE OR DELETE
      ON security_role_permissions FOR EACH ROW EXECUTE FUNCTION guard_security_role_permission();
    """)


def downgrade():
    # Existing protected data must never silently become readable legacy data.
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM assignments WHERE "
            "security_classification <> 'legacy_homework') OR "
            "EXISTS(SELECT 1 FROM security_audit_events) OR "
            "EXISTS(SELECT 1 FROM security_grants) OR "
            "EXISTS(SELECT 1 FROM security_scopes WHERE kind='assignment') OR "
            "EXISTS(SELECT 1 FROM security_role_versions WHERE id NOT IN "
            "('11111111-b11a-4000-8000-000000000002'::uuid, "
            "'11111111-b11a-4000-8000-000000000003'::uuid, "
            "'11111111-b11a-4000-8000-000000000004'::uuid))"
        )
    ):
        raise RuntimeError("resource_security_downgrade_requires_archived_new_data")
    for table in (
        "security_audit_events",
        "security_grants",
        "security_role_permissions",
        "security_role_versions",
        "security_scopes",
        "permission_definitions",
        "security_installations",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION guard_security_role_permission()")
    op.execute("DROP FUNCTION guard_security_role()")
    op.execute("DROP FUNCTION guard_security_grant()")
    op.execute("DROP FUNCTION guard_security_immutable()")
    op.execute("DROP TRIGGER immutable_assignment_classification ON assignments")
    op.execute("DROP FUNCTION protect_assignment_classification()")
    op.drop_constraint("ck_assignments_security_classification", "assignments", type_="check")
    op.drop_column("assignments", "security_classification")
