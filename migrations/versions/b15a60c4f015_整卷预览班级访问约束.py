"""Narrow preview/export access to explicit classes without persisting scores."""

import sqlalchemy as sa
from alembic import op

revision = "b15a60c4f015"
down_revision = "b14c52b3f014"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "exam_result_class_access",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "grant_id",
            sa.Uuid(),
            sa.ForeignKey("security_grants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "exam_session_id",
            sa.Uuid(),
            sa.ForeignKey("exam_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "administrative_class_id",
            sa.Uuid(),
            sa.ForeignKey("administrative_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("export_allowed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "uq_result_class_access_active",
        "exam_result_class_access",
        ["grant_id", "exam_session_id", "administrative_class_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.execute("""CREATE FUNCTION guard_exam_result_class_access() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'result_class_access_history_immutable' USING ERRCODE='23514';
      END IF;
      IF TG_OP='UPDATE' THEN
        IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR
           (to_jsonb(NEW)-'revoked_at') IS DISTINCT FROM (to_jsonb(OLD)-'revoked_at')
        THEN RAISE EXCEPTION 'result_class_access_immutable' USING ERRCODE='23514'; END IF;
        RETURN NEW;
      END IF;
      IF NEW.revoked_at IS NOT NULL OR NOT EXISTS (
        SELECT 1 FROM security_grants g JOIN security_scopes s ON s.id=g.scope_id
        JOIN exam_sessions es ON es.id=NEW.exam_session_id
        JOIN users u ON u.id=g.user_id
        WHERE g.id=NEW.grant_id AND g.revoked_at IS NULL AND u.is_active
        AND (s.kind='installation' OR (s.kind='exam_session' AND s.exam_session_id=es.id)
             OR (s.kind='exam_event' AND s.exam_event_id=es.exam_event_id))
      ) OR NOT EXISTS (
        SELECT 1 FROM roster_entry_snapshots re
        JOIN roster_revisions rr ON rr.id=re.roster_revision_id
        WHERE rr.exam_session_id=NEW.exam_session_id AND rr.is_current AND rr.status='frozen'
          AND re.administrative_class_id=NEW.administrative_class_id
      ) THEN RAISE EXCEPTION 'result_class_access_scope_mismatch' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER guard_result_class_access BEFORE INSERT OR UPDATE OR DELETE ON "
        "exam_result_class_access FOR EACH ROW EXECUTE FUNCTION guard_exam_result_class_access()"
    )


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM exam_result_class_access)
        THEN RAISE EXCEPTION 'result class access facts prevent destructive downgrade';
        END IF; END $$""")
    op.execute("DROP TRIGGER guard_result_class_access ON exam_result_class_access")
    op.execute("DROP FUNCTION guard_exam_result_class_access()")
    op.drop_table("exam_result_class_access")
