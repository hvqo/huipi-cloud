"""Single-marker task allocation, immutable assignment generations and submitted review links."""

# ruff: noqa: E501
import sqlalchemy as sa
from alembic import op

revision = "b16a82e6a017"
down_revision = "b15b71d5f016"
branch_labels = None
depends_on = None

MANAGER = "11111111-b16a-4000-8000-000000000001"
TEACHER = "11111111-b16a-4000-8000-000000000002"
DEPLOYER = "11111111-b16a-4000-8000-000000000003"


def upgrade():
    op.create_table(
        "marking_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("exam_session_id", sa.Uuid(), sa.ForeignKey("exam_sessions.id"), nullable=False),
        sa.Column(
            "workspace_id", sa.Uuid(), sa.ForeignKey("exam_grading_workspaces.id"), nullable=False
        ),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), sa.ForeignKey("marking_tasks.id")),
        sa.Column("assignee_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assigned_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="assigned"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("review_id", sa.Uuid(), sa.ForeignKey("grade_review_revisions.review_id")),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.UniqueConstraint("workspace_id", "generation", name="uq_marking_generation"),
        sa.UniqueConstraint("supersedes_id", name="uq_marking_successor"),
        sa.UniqueConstraint("review_id", name="uq_marking_final_review"),
        sa.CheckConstraint("generation>0 AND revision>0", name="ck_marking_revision"),
        sa.CheckConstraint(
            "status IN ('assigned','in_progress','completed','cancelled','superseded')",
            name="ck_marking_status",
        ),
        sa.CheckConstraint("is_current=(status<>'superseded')", name="ck_marking_current"),
        sa.CheckConstraint(
            "(status='completed' AND review_id IS NOT NULL AND completed_at IS NOT NULL) OR (status='superseded') OR (status<>'completed' AND review_id IS NULL AND completed_at IS NULL)",
            name="ck_marking_result",
        ),
    )
    op.create_index(
        "uq_marking_current",
        "marking_tasks",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index("ix_marking_assignee", "marking_tasks", ["assignee_user_id", "is_current"])
    from sqlalchemy.dialects.postgresql import JSONB

    op.create_table(
        "marking_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("exam_session_id", sa.Uuid(), sa.ForeignKey("exam_sessions.id"), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("actor_user_id", "request_id", name="uq_marking_operation"),
        sa.CheckConstraint("length(payload_sha256)=64", name="ck_marking_operation_digest"),
    )
    for statement in GUARD.strip().split(";\nCREATE FUNCTION"):
        op.execute(
            statement if statement.startswith("CREATE FUNCTION") else "CREATE FUNCTION" + statement
        )
    op.execute(
        "CREATE TRIGGER protect_marking_task BEFORE INSERT OR UPDATE OR DELETE ON marking_tasks FOR EACH ROW EXECUTE FUNCTION guard_marking_task()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER current_marking_task AFTER INSERT OR UPDATE ON marking_tasks DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_marking_current()"
    )
    op.execute(
        "CREATE TRIGGER immutable_marking_operation BEFORE UPDATE OR DELETE ON marking_operations FOR EACH ROW EXECUTE FUNCTION guard_security_immutable()"
    )
    db = op.get_bind()
    for code in ["exam.task.manage", "exam.task.work"]:
        db.execute(
            sa.text(
                "INSERT INTO permission_definitions VALUES (:code,true,ARRAY['installation','exam_event','exam_session'])"
            ),
            dict(code=code),
        )
    manager_codes = list(
        db.execute(
            sa.text(
                "SELECT permission_code FROM security_role_permissions WHERE role_version_id='11111111-b14c-4000-8000-000000000001'"
            )
        ).scalars()
    ) + ["exam.task.manage"]
    for role, name, codes in [
        (MANAGER, "marking_manager", manager_codes),
        (TEACHER, "marking_teacher", ["exam.task.work"]),
        (
            DEPLOYER,
            "marking_deployer",
            manager_codes + ["exam.task.work", "security.grants.manage"],
        ),
    ]:
        db.execute(
            sa.text("INSERT INTO security_role_versions VALUES (CAST(:id AS uuid),:name,1,NULL)"),
            dict(id=role, name=name),
        )
        for code in sorted(set(codes)):
            db.execute(
                sa.text("INSERT INTO security_role_permissions VALUES (CAST(:id AS uuid),:code)"),
                dict(id=role, code=code),
            )
        db.execute(
            sa.text(
                "UPDATE security_role_versions SET sealed_at=clock_timestamp() WHERE id=CAST(:id AS uuid)"
            ),
            dict(id=role),
        )


GUARD = """
CREATE FUNCTION guard_marking_task() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prev marking_tasks; BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'marking_history_immutable' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM exam_grading_workspaces w JOIN exam_parsing_jobs j ON j.id=w.parsing_job_id
  WHERE w.id=NEW.workspace_id AND j.exam_session_id=NEW.exam_session_id)
 THEN RAISE EXCEPTION 'marking_workspace_session_mismatch' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.status<>'assigned' OR NOT NEW.is_current OR NEW.revision<>1 OR NEW.review_id IS NOT NULL
   OR NEW.started_at IS NOT NULL OR NEW.completed_at IS NOT NULL
   THEN RAISE EXCEPTION 'marking_initial_state_invalid' USING ERRCODE='23514'; END IF;
  IF NEW.supersedes_id IS NULL THEN
   IF NEW.generation<>1 THEN RAISE EXCEPTION 'marking_generation_invalid' USING ERRCODE='23514'; END IF;
  ELSE
   SELECT * INTO prev FROM marking_tasks WHERE id=NEW.supersedes_id FOR UPDATE;
   IF prev.id IS NULL OR prev.workspace_id<>NEW.workspace_id OR prev.exam_session_id<>NEW.exam_session_id
    OR prev.generation+1<>NEW.generation OR prev.is_current OR prev.status<>'superseded'
   THEN RAISE EXCEPTION 'marking_predecessor_invalid' USING ERRCODE='23514'; END IF;
  END IF;
 ELSE
  IF NOT OLD.is_current OR NEW.id<>OLD.id OR NEW.workspace_id<>OLD.workspace_id
   OR NEW.exam_session_id<>OLD.exam_session_id OR NEW.generation<>OLD.generation
   OR NEW.supersedes_id IS DISTINCT FROM OLD.supersedes_id
   OR NEW.assignee_user_id<>OLD.assignee_user_id OR NEW.assigned_by_user_id<>OLD.assigned_by_user_id
   OR NEW.assigned_at<>OLD.assigned_at OR NEW.reason<>OLD.reason OR NEW.revision<>OLD.revision+1
  THEN RAISE EXCEPTION 'marking_assignment_immutable' USING ERRCODE='23514'; END IF;
  IF NOT ((OLD.status='assigned' AND NEW.status IN ('in_progress','cancelled','superseded'))
   OR (OLD.status='in_progress' AND NEW.status IN ('completed','cancelled','superseded'))
   OR (OLD.status IN ('completed','cancelled') AND NEW.status='superseded'))
  THEN RAISE EXCEPTION 'marking_transition_invalid' USING ERRCODE='23514'; END IF;
  IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at
  THEN RAISE EXCEPTION 'marking_started_immutable' USING ERRCODE='23514'; END IF;
  IF NEW.status='superseded' AND (NEW.review_id IS DISTINCT FROM OLD.review_id
    OR NEW.completed_at IS DISTINCT FROM OLD.completed_at)
  THEN RAISE EXCEPTION 'marking_review_immutable' USING ERRCODE='23514'; END IF;
 END IF;
 IF NEW.status='completed' AND NOT EXISTS (
  SELECT 1 FROM grade_review_revisions r JOIN exam_scoring_jobs s ON s.proposal_id=r.proposal_id
  JOIN exam_grading_workspaces w ON w.id=s.workspace_id
  WHERE r.review_id=NEW.review_id AND w.id=NEW.workspace_id AND r.question_id=w.question_id
   AND r.review_status IN ('accepted','revised') AND r.reviewer_user_id=NEW.assignee_user_id
   AND r.created_at>=NEW.assigned_at AND s.status='succeeded'
   AND NOT EXISTS(SELECT 1 FROM grade_review_revisions newer WHERE newer.proposal_id=r.proposal_id AND newer.review_revision>r.review_revision)
 ) THEN RAISE EXCEPTION 'marking_review_binding_invalid' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION check_marking_current() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (SELECT count(*) FROM marking_tasks WHERE workspace_id=NEW.workspace_id AND is_current)<>1
  OR NOT EXISTS(SELECT 1 FROM marking_tasks t WHERE t.workspace_id=NEW.workspace_id AND t.is_current
   AND t.generation=(SELECT max(generation) FROM marking_tasks WHERE workspace_id=NEW.workspace_id))
 THEN RAISE EXCEPTION 'marking_current_invalid' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
"""


def downgrade():
    op.execute(
        "LOCK TABLE marking_tasks,marking_operations,security_grants IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(f"""DO $$ BEGIN IF EXISTS(SELECT 1 FROM marking_tasks) OR EXISTS(SELECT 1 FROM marking_operations)
      OR EXISTS(SELECT 1 FROM security_grants WHERE role_version_id IN ('{MANAGER}','{TEACHER}','{DEPLOYER}'))
      THEN RAISE EXCEPTION 'marking_facts_prevent_downgrade'; END IF; END $$""")
    op.drop_table("marking_operations")
    op.drop_table("marking_tasks")
    op.execute("DROP FUNCTION guard_marking_task()")
    op.execute("DROP FUNCTION check_marking_current()")
    for table, trigger in [
        ("security_role_permissions", "immutable_security_role_permissions"),
        ("security_role_versions", "immutable_security_role"),
    ]:
        op.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
    op.execute(
        f"DELETE FROM security_role_permissions WHERE role_version_id IN ('{MANAGER}','{TEACHER}','{DEPLOYER}')"
    )
    op.execute(
        f"DELETE FROM security_role_versions WHERE id IN ('{MANAGER}','{TEACHER}','{DEPLOYER}')"
    )
    for table, trigger in [
        ("security_role_permissions", "immutable_security_role_permissions"),
        ("security_role_versions", "immutable_security_role"),
    ]:
        op.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
    op.execute(
        "DELETE FROM permission_definitions WHERE code IN ('exam.task.manage','exam.task.work')"
    )
