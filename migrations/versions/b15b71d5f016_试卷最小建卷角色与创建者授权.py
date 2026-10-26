"""Narrow creation role and transaction-bound, object-only creator authority."""

import sqlalchemy as sa
from alembic import op

revision = "b15b71d5f016"
down_revision = "b15a60c4f015"
branch_labels = None
depends_on = None

CREATOR = "11111111-b15b-4000-8000-000000000001"
AUTHOR = "11111111-b15b-4000-8000-000000000002"


def upgrade():
    bind = op.get_bind()
    for identifier, name, codes in [
        (CREATOR, "paper_creator", ["paper.create"]),
        (AUTHOR, "paper_author", ["paper.read", "paper.edit", "paper.lock"]),
    ]:
        bind.execute(
            sa.text("INSERT INTO security_role_versions VALUES (CAST(:id AS uuid),:code,1,NULL)"),
            dict(id=identifier, code=name),
        )
        for code in codes:
            bind.execute(
                sa.text("INSERT INTO security_role_permissions VALUES (CAST(:id AS uuid),:code)"),
                dict(id=identifier, code=code),
            )
        bind.execute(
            sa.text(
                "UPDATE security_role_versions SET sealed_at=clock_timestamp() "
                "WHERE id=CAST(:id AS uuid)"
            ),
            dict(id=identifier),
        )
    op.create_table(
        "paper_creator_authorities",
        sa.Column(
            "paper_version_id",
            sa.Uuid(),
            sa.ForeignKey("paper_versions.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "grant_id",
            sa.Uuid(),
            sa.ForeignKey("security_grants.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "operation_id",
            sa.Uuid(),
            sa.ForeignKey(
                "paper_operations.id", ondelete="RESTRICT", deferrable=True, initially="DEFERRED"
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.execute("""CREATE TRIGGER immutable_paper_creator_authority BEFORE UPDATE OR DELETE
      ON paper_creator_authorities FOR EACH ROW EXECUTE FUNCTION guard_security_immutable()""")
    op.execute(f"""CREATE FUNCTION guard_paper_role_scope() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.role_version_id='{CREATOR}' AND NOT EXISTS (
        SELECT 1 FROM security_scopes WHERE id=NEW.scope_id AND kind='installation'
      ) THEN RAISE EXCEPTION 'paper_creator_scope_invalid' USING ERRCODE='23514'; END IF;
      IF NEW.role_version_id='{AUTHOR}' AND NOT EXISTS (
        SELECT 1 FROM security_scopes s JOIN paper_versions p ON p.id=s.paper_version_id
        WHERE s.id=NEW.scope_id AND s.kind='paper_version' AND p.created_by_user_id=NEW.user_id
      ) THEN RAISE EXCEPTION 'paper_author_scope_invalid' USING ERRCODE='23514'; END IF;
      IF TG_OP='INSERT' AND NEW.role_version_id='{AUTHOR}' AND NOT EXISTS (
        SELECT 1 FROM security_scopes s JOIN paper_versions p ON p.id=s.paper_version_id
        WHERE s.id=NEW.scope_id AND p.status='draft' AND p.revision=1
          AND p.created_at=transaction_timestamp()
      ) THEN RAISE EXCEPTION 'paper_author_requires_new_version' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("""CREATE TRIGGER paper_role_scope BEFORE INSERT OR UPDATE ON security_grants
      FOR EACH ROW EXECUTE FUNCTION guard_paper_role_scope()""")
    op.execute(f"""CREATE FUNCTION check_paper_creator_authority() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE target uuid;
    BEGIN
      IF TG_TABLE_NAME='security_grants' THEN
        IF NEW.role_version_id<>'{AUTHOR}' THEN RETURN NULL; END IF;
        SELECT paper_version_id INTO target FROM paper_creator_authorities WHERE grant_id=NEW.id;
      ELSE target := NEW.paper_version_id; END IF;
      IF target IS NULL OR NOT EXISTS (
        SELECT 1 FROM paper_creator_authorities ca
        JOIN paper_versions p ON p.id=ca.paper_version_id
        JOIN security_grants g ON g.id=ca.grant_id
        JOIN security_scopes s ON s.id=g.scope_id
        JOIN paper_operations o ON o.id=ca.operation_id
        WHERE ca.paper_version_id=target AND g.user_id=ca.actor_user_id
          AND p.created_by_user_id=ca.actor_user_id AND o.actor_user_id=ca.actor_user_id
          AND g.role_version_id='{AUTHOR}' AND s.kind='paper_version' AND s.paper_version_id=p.id
          AND o.action IN ('snapshot','fork') AND o.result->>'paper_version_id'=p.id::text
          AND p.created_at=o.created_at AND p.created_at=ca.created_at AND p.created_at=g.created_at
      ) THEN RAISE EXCEPTION 'paper_creator_authority_invalid' USING ERRCODE='23514'; END IF;
      RETURN NULL;
    END $$""")
    op.execute("""CREATE CONSTRAINT TRIGGER check_new_paper_creator_authority
      AFTER INSERT ON paper_creator_authorities DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION check_paper_creator_authority()""")
    op.execute("""CREATE CONSTRAINT TRIGGER check_new_paper_author_grant
      AFTER INSERT ON security_grants DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW EXECUTE FUNCTION check_paper_creator_authority()""")


def downgrade():
    op.execute("""LOCK TABLE paper_creator_authorities,security_grants,
      security_role_permissions,security_role_versions IN ACCESS EXCLUSIVE MODE""")
    op.execute(f"""DO $$ BEGIN IF EXISTS(SELECT 1 FROM paper_creator_authorities)
      OR EXISTS(SELECT 1 FROM security_grants WHERE role_version_id IN ('{CREATOR}','{AUTHOR}'))
      THEN RAISE EXCEPTION 'paper_creator_facts_prevent_downgrade'; END IF; END $$""")
    op.execute("DROP TRIGGER check_new_paper_author_grant ON security_grants")
    op.execute("DROP TRIGGER check_new_paper_creator_authority ON paper_creator_authorities")
    op.execute("DROP TRIGGER paper_role_scope ON security_grants")
    op.execute("DROP FUNCTION check_paper_creator_authority()")
    op.execute("DROP FUNCTION guard_paper_role_scope()")
    op.drop_table("paper_creator_authorities")
    for trigger, table in [
        ("immutable_security_role_permissions", "security_role_permissions"),
        ("immutable_security_role", "security_role_versions"),
    ]:
        op.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
    op.execute(
        f"DELETE FROM security_role_permissions WHERE role_version_id IN ('{CREATOR}','{AUTHOR}')"
    )
    op.execute(f"DELETE FROM security_role_versions WHERE id IN ('{CREATOR}','{AUTHOR}')")
    for trigger, table in [
        ("immutable_security_role_permissions", "security_role_permissions"),
        ("immutable_security_role", "security_role_versions"),
    ]:
        op.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
