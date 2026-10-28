"""Append-only attendance and approved exam score versions."""
# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "b19a15a0c020"
down_revision = "b18a04f8c019"
branch_labels = None
depends_on = None

MANAGER = "11111111-b19a-4000-8000-000000000001"
DEPLOYER = "11111111-b19a-4000-8000-000000000002"
ACTIONS = ("exam.result.manage", "exam.result.approve", "exam.result.publish")
BASE = (
    "exam.mark.read",
    "exam.roster.read",
    "exam.script.read",
    "exam.identity.verify",
    "exam.score.review",
)


def upgrade():
    op.create_table(
        "exam_attendance_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "exam_session_id",
            sa.Uuid(),
            sa.ForeignKey("exam_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "roster_revision_id",
            sa.Uuid(),
            sa.ForeignKey("roster_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "roster_entry_id",
            sa.Uuid(),
            sa.ForeignKey("roster_entry_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "participation_id",
            sa.Uuid(),
            sa.ForeignKey("exam_participations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "exam_session_id", "participation_id", "revision", name="uq_exam_attendance_revision"
        ),
        sa.CheckConstraint("revision > 0", name="ck_exam_attendance_revision"),
        sa.CheckConstraint(
            "status IN ('present','absent','unverified')", name="ck_exam_attendance_status"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 300", name="ck_exam_attendance_reason"
        ),
    )
    op.create_table(
        "exam_final_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "exam_session_id",
            sa.Uuid(),
            sa.ForeignKey("exam_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "roster_revision_id",
            sa.Uuid(),
            sa.ForeignKey("roster_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "supersedes_id", sa.Uuid(), sa.ForeignKey("exam_final_versions.id", ondelete="RESTRICT")
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("approved_by_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column(
            "published_by_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "creation_txid", sa.BigInteger(), nullable=False, server_default=sa.func.txid_current()
        ),
        sa.UniqueConstraint("exam_session_id", "version_no", name="uq_exam_final_version"),
        sa.CheckConstraint("version_no>0 AND revision>0", name="ck_exam_final_revision"),
        sa.CheckConstraint(
            "status IN ('pending','approved','published','cancelled')", name="ck_exam_final_status"
        ),
        sa.CheckConstraint(
            "length(snapshot_sha256)=64 AND length(trim(reason)) BETWEEN 1 AND 300",
            name="ck_exam_final_digest",
        ),
    )
    op.create_index(
        "uq_exam_final_pending",
        "exam_final_versions",
        ["exam_session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','approved')"),
    )
    op.create_table(
        "exam_final_students",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "version_id",
            sa.Uuid(),
            sa.ForeignKey("exam_final_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "participation_id",
            sa.Uuid(),
            sa.ForeignKey("exam_participations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "roster_entry_id",
            sa.Uuid(),
            sa.ForeignKey("roster_entry_snapshots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "attendance_id",
            sa.Uuid(),
            sa.ForeignKey("exam_attendance_decisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "class_id",
            sa.Uuid(),
            sa.ForeignKey("administrative_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "script_version_id",
            sa.Uuid(),
            sa.ForeignKey("answer_script_versions.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "session_paper_id", sa.Uuid(), sa.ForeignKey("session_papers.id", ondelete="RESTRICT")
        ),
        sa.Column(
            "paper_version_id", sa.Uuid(), sa.ForeignKey("paper_versions.id", ondelete="RESTRICT")
        ),
        sa.Column("attendance", sa.String(16), nullable=False),
        sa.Column("total", sa.Numeric(8, 2)),
        sa.Column("max_points", sa.Numeric(8, 2)),
        sa.UniqueConstraint("version_id", "participation_id", name="uq_exam_final_student"),
        sa.CheckConstraint("attendance IN ('present','absent')", name="ck_exam_final_attendance"),
        sa.CheckConstraint(
            "(attendance='absent' AND total IS NULL AND script_version_id IS NULL) OR (attendance='present' AND total IS NOT NULL AND script_version_id IS NOT NULL AND total>=0 AND max_points>0 AND total<=max_points)",
            name="ck_exam_final_student_score",
        ),
    )
    op.create_table(
        "exam_final_questions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "student_result_id",
            sa.Uuid(),
            sa.ForeignKey("exam_final_students.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "mapping_id",
            sa.Uuid(),
            sa.ForeignKey("paper_question_mappings.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "review_id",
            sa.Uuid(),
            sa.ForeignKey("grade_review_revisions.review_id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "omr_decision_id", sa.Uuid(), sa.ForeignKey("omr_decisions.id", ondelete="RESTRICT")
        ),
        sa.Column("question_number", sa.Integer(), nullable=False),
        sa.Column("points", sa.Numeric(8, 2), nullable=False),
        sa.Column("max_points", sa.Numeric(8, 2), nullable=False),
        sa.UniqueConstraint("student_result_id", "mapping_id", name="uq_exam_final_question"),
        sa.CheckConstraint(
            "points>=0 AND max_points>0 AND points<=max_points", name="ck_exam_final_question_score"
        ),
        sa.CheckConstraint(
            "(review_id IS NOT NULL AND omr_decision_id IS NULL) OR (review_id IS NULL AND omr_decision_id IS NOT NULL)",
            name="ck_exam_final_question_evidence",
        ),
    )
    op.create_table(
        "exam_final_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "exam_session_id",
            sa.Uuid(),
            sa.ForeignKey("exam_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("actor_user_id", "request_id", name="uq_exam_final_operation"),
        sa.CheckConstraint("length(payload_sha256)=64", name="ck_exam_final_operation_sha"),
    )
    for statement in GUARDS.strip().split(";\nCREATE FUNCTION"):
        op.execute(
            statement if statement.startswith("CREATE FUNCTION") else "CREATE FUNCTION" + statement
        )
    for table, fn in [
        ("exam_attendance_decisions", "guard_final_attendance"),
        ("exam_final_versions", "guard_final_version"),
        ("exam_final_students", "guard_final_student"),
        ("exam_final_questions", "guard_final_question"),
    ]:
        op.execute(
            f"CREATE TRIGGER protect_{table} BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION {fn}()"
        )
    op.execute(
        "CREATE TRIGGER immutable_exam_final_operation BEFORE UPDATE OR DELETE ON exam_final_operations FOR EACH ROW EXECUTE FUNCTION guard_security_immutable()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER final_snapshot_complete AFTER INSERT ON exam_final_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_final_snapshot_complete()"
    )
    db = op.get_bind()
    for code in ACTIONS:
        db.execute(
            sa.text(
                "INSERT INTO permission_definitions VALUES (:code,true,ARRAY['installation','exam_event','exam_session'])"
            ),
            {"code": code},
        )
    for role, name, codes in [
        (MANAGER, "exam_result_manager", (*BASE, *ACTIONS)),
        (DEPLOYER, "exam_result_deployer", (*BASE, *ACTIONS, "security.grants.manage")),
    ]:
        db.execute(
            sa.text("INSERT INTO security_role_versions VALUES (CAST(:id AS uuid),:name,1,NULL)"),
            {"id": role, "name": name},
        )
        for code in sorted(set(codes)):
            db.execute(
                sa.text("INSERT INTO security_role_permissions VALUES (CAST(:id AS uuid),:code)"),
                {"id": role, "code": code},
            )
        db.execute(
            sa.text(
                "UPDATE security_role_versions SET sealed_at=clock_timestamp() WHERE id=CAST(:id AS uuid)"
            ),
            {"id": role},
        )


GUARDS = r"""
CREATE FUNCTION guard_final_attendance() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE next_rev integer; BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'attendance_history_immutable' USING ERRCODE='23514'; END IF;
 PERFORM 1 FROM exam_sessions WHERE id=NEW.exam_session_id FOR UPDATE;
 SELECT coalesce(max(revision),0)+1 INTO next_rev FROM exam_attendance_decisions WHERE exam_session_id=NEW.exam_session_id AND participation_id=NEW.participation_id;
 IF NEW.revision<>next_rev OR NOT EXISTS(SELECT 1 FROM roster_entry_snapshots e JOIN roster_revisions r ON r.id=e.roster_revision_id WHERE e.id=NEW.roster_entry_id AND e.participation_id=NEW.participation_id AND e.exam_session_id=NEW.exam_session_id AND r.id=NEW.roster_revision_id AND r.exam_session_id=NEW.exam_session_id AND r.status='frozen' AND r.is_current)
 THEN RAISE EXCEPTION 'attendance_roster_mismatch' USING ERRCODE='23514'; END IF;
 IF NEW.status='absent' AND EXISTS(SELECT 1 FROM answer_scripts WHERE exam_session_id=NEW.exam_session_id AND current_participation_id=NEW.participation_id)
 THEN RAISE EXCEPTION 'attendance_conflicts_with_script' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION guard_final_version() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n integer; last_published uuid; BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'final_history_immutable' USING ERRCODE='23514'; END IF;
 PERFORM 1 FROM exam_sessions WHERE id=NEW.exam_session_id FOR UPDATE;
 IF TG_OP='INSERT' THEN
  SELECT coalesce(max(version_no),0)+1 INTO n FROM exam_final_versions WHERE exam_session_id=NEW.exam_session_id;
  SELECT id INTO last_published FROM exam_final_versions WHERE exam_session_id=NEW.exam_session_id AND status='published' ORDER BY version_no DESC LIMIT 1;
  IF NEW.version_no<>n OR NEW.revision<>1 OR NEW.status<>'pending' OR NEW.supersedes_id IS DISTINCT FROM last_published OR NEW.approved_at IS NOT NULL OR NEW.published_at IS NOT NULL OR NEW.approved_by_user_id IS NOT NULL OR NEW.published_by_user_id IS NOT NULL OR NOT EXISTS(SELECT 1 FROM roster_revisions WHERE id=NEW.roster_revision_id AND exam_session_id=NEW.exam_session_id AND status='frozen' AND is_current)
  THEN RAISE EXCEPTION 'final_version_invalid' USING ERRCODE='23514'; END IF;
 ELSE
  IF (NEW.id,NEW.exam_session_id,NEW.roster_revision_id,NEW.version_no,NEW.supersedes_id,NEW.snapshot_sha256,NEW.reason,NEW.created_by_user_id,NEW.created_at,NEW.creation_txid) IS DISTINCT FROM (OLD.id,OLD.exam_session_id,OLD.roster_revision_id,OLD.version_no,OLD.supersedes_id,OLD.snapshot_sha256,OLD.reason,OLD.created_by_user_id,OLD.created_at,OLD.creation_txid) OR NEW.revision<>OLD.revision+1
  THEN RAISE EXCEPTION 'final_version_immutable' USING ERRCODE='23514'; END IF;
  IF NOT ((OLD.status='pending' AND NEW.status IN ('approved','cancelled')) OR (OLD.status='approved' AND NEW.status IN ('published','cancelled')))
  THEN RAISE EXCEPTION 'final_transition_invalid' USING ERRCODE='23514'; END IF;
  IF NEW.status='approved' AND (NEW.approved_by_user_id IS NULL OR NEW.approved_at IS NULL OR NEW.published_at IS NOT NULL OR NEW.published_by_user_id IS NOT NULL) THEN RAISE EXCEPTION 'final_approval_invalid' USING ERRCODE='23514'; END IF;
  IF NEW.status='published' AND (NEW.approved_by_user_id IS DISTINCT FROM OLD.approved_by_user_id OR NEW.approved_at IS DISTINCT FROM OLD.approved_at OR NEW.published_by_user_id IS NULL OR NEW.published_at IS NULL) THEN RAISE EXCEPTION 'final_publication_invalid' USING ERRCODE='23514'; END IF;
  IF NEW.status='cancelled' AND (NEW.approved_by_user_id,NEW.approved_at,NEW.published_by_user_id,NEW.published_at) IS DISTINCT FROM (OLD.approved_by_user_id,OLD.approved_at,OLD.published_by_user_id,OLD.published_at) THEN RAISE EXCEPTION 'final_cancellation_invalid' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION guard_final_student() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'final_student_immutable' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM exam_final_versions v JOIN roster_entry_snapshots e ON e.id=NEW.roster_entry_id JOIN exam_attendance_decisions a ON a.id=NEW.attendance_id WHERE v.id=NEW.version_id AND v.creation_txid=txid_current() AND v.status='pending' AND e.roster_revision_id=v.roster_revision_id AND e.exam_session_id=v.exam_session_id AND e.participation_id=NEW.participation_id AND e.administrative_class_id=NEW.class_id AND a.roster_entry_id=e.id AND a.participation_id=e.participation_id AND a.status=NEW.attendance AND NOT EXISTS(SELECT 1 FROM exam_attendance_decisions newer WHERE newer.exam_session_id=v.exam_session_id AND newer.participation_id=e.participation_id AND newer.revision>a.revision))
 THEN RAISE EXCEPTION 'final_student_roster_mismatch' USING ERRCODE='23514'; END IF;
 IF NEW.attendance='present' AND NOT EXISTS(SELECT 1 FROM answer_script_versions sv JOIN answer_scripts s ON s.id=sv.answer_script_id JOIN session_papers sp ON sp.id=sv.session_paper_id WHERE sv.id=NEW.script_version_id AND s.current_version_id=sv.id AND sv.status='confirmed' AND sv.completeness='complete' AND sv.participation_id=NEW.participation_id AND sv.roster_entry_id=NEW.roster_entry_id AND sp.id=NEW.session_paper_id AND sp.paper_version_id=NEW.paper_version_id)
 THEN RAISE EXCEPTION 'final_student_script_mismatch' USING ERRCODE='23514'; END IF;
 IF NEW.attendance='absent' AND EXISTS(SELECT 1 FROM answer_scripts s JOIN exam_final_versions v ON v.id=NEW.version_id WHERE s.exam_session_id=v.exam_session_id AND s.current_participation_id=NEW.participation_id)
 THEN RAISE EXCEPTION 'final_absence_conflicts_with_script' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION guard_final_question() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'final_question_immutable' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM exam_final_students s JOIN exam_final_versions v ON v.id=s.version_id JOIN paper_question_mappings m ON m.id=NEW.mapping_id JOIN questions q ON q.id=m.legacy_question_id WHERE s.id=NEW.student_result_id AND s.attendance='present' AND v.creation_txid=txid_current() AND m.paper_version_id=s.paper_version_id AND q.max_score=NEW.max_points AND q.question_number=NEW.question_number)
 THEN RAISE EXCEPTION 'final_question_mapping_mismatch' USING ERRCODE='23514'; END IF;
 IF NEW.review_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM grade_review_revisions r JOIN exam_scoring_jobs sj ON sj.proposal_id=r.proposal_id JOIN exam_grading_workspaces w ON w.id=sj.workspace_id JOIN exam_parsing_jobs pj ON pj.id=w.parsing_job_id JOIN exam_final_students s ON s.id=NEW.student_result_id WHERE r.review_id=NEW.review_id AND r.review_status IN ('accepted','revised') AND r.reviewed_total_score=NEW.points AND w.paper_question_mapping_id=NEW.mapping_id AND pj.answer_script_version_id=s.script_version_id AND NOT EXISTS(SELECT 1 FROM grade_review_revisions newer WHERE newer.proposal_id=r.proposal_id AND newer.review_revision>r.review_revision))
 THEN RAISE EXCEPTION 'final_question_review_mismatch' USING ERRCODE='23514'; END IF;
 IF NEW.omr_decision_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM omr_decisions d JOIN omr_recognitions rec ON rec.id=d.recognition_id JOIN exam_final_students s ON s.id=NEW.student_result_id WHERE d.id=NEW.omr_decision_id AND d.mapping_id=NEW.mapping_id AND d.points=NEW.points AND rec.script_version_id=s.script_version_id AND NOT EXISTS(SELECT 1 FROM omr_decisions newer WHERE newer.recognition_id=d.recognition_id AND newer.mapping_id=d.mapping_id AND newer.revision>d.revision))
 THEN RAISE EXCEPTION 'final_question_omr_mismatch' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION check_final_snapshot_complete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (SELECT count(*) FROM exam_final_students WHERE version_id=NEW.id)<>(SELECT count(*) FROM roster_entry_snapshots WHERE roster_revision_id=NEW.roster_revision_id)
 OR EXISTS(SELECT 1 FROM exam_final_students s WHERE s.version_id=NEW.id AND ((s.attendance='absent' AND EXISTS(SELECT 1 FROM exam_final_questions q WHERE q.student_result_id=s.id)) OR (s.attendance='present' AND ((SELECT count(*) FROM exam_final_questions q WHERE q.student_result_id=s.id)<>(SELECT count(*) FROM paper_question_mappings m WHERE m.paper_version_id=s.paper_version_id) OR (SELECT coalesce(sum(q.points),0) FROM exam_final_questions q WHERE q.student_result_id=s.id)<>s.total OR (SELECT coalesce(sum(q.max_points),0) FROM exam_final_questions q WHERE q.student_result_id=s.id)<>s.max_points))))
 THEN RAISE EXCEPTION 'final_snapshot_incomplete' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
"""


def downgrade():
    db = op.get_bind()
    if db.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM exam_final_versions) OR EXISTS(SELECT 1 FROM exam_attendance_decisions) OR EXISTS(SELECT 1 FROM exam_final_operations) OR EXISTS(SELECT 1 FROM security_grants WHERE role_version_id IN (CAST(:a AS uuid), CAST(:b AS uuid)))"
        ),
        {"a": MANAGER, "b": DEPLOYER},
    ).scalar():
        raise RuntimeError("存在正式成绩或授权事实，禁止破坏性降级")
    for table in (
        "exam_final_operations",
        "exam_final_questions",
        "exam_final_students",
        "exam_final_versions",
        "exam_attendance_decisions",
    ):
        op.drop_table(table)
    for fn in (
        "check_final_snapshot_complete",
        "guard_final_question",
        "guard_final_student",
        "guard_final_version",
        "guard_final_attendance",
    ):
        op.execute(f"DROP FUNCTION {fn}()")
    op.execute(
        "ALTER TABLE security_role_permissions DISABLE TRIGGER immutable_security_role_permissions"
    )
    op.execute("ALTER TABLE security_role_versions DISABLE TRIGGER immutable_security_role")
    db.execute(
        sa.text(
            "DELETE FROM security_role_permissions WHERE role_version_id IN (CAST(:a AS uuid), CAST(:b AS uuid))"
        ),
        {"a": MANAGER, "b": DEPLOYER},
    )
    db.execute(
        sa.text(
            "DELETE FROM security_role_versions WHERE id IN (CAST(:a AS uuid), CAST(:b AS uuid))"
        ),
        {"a": MANAGER, "b": DEPLOYER},
    )
    op.execute(
        "ALTER TABLE security_role_permissions ENABLE TRIGGER immutable_security_role_permissions"
    )
    op.execute("ALTER TABLE security_role_versions ENABLE TRIGGER immutable_security_role")
    db.execute(
        sa.text("DELETE FROM permission_definitions WHERE code = ANY(:codes)"),
        {"codes": list(ACTIONS)},
    )
