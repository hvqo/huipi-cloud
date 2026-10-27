"""Add source-bound image regions to the existing exam scoring and review pipeline."""
# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op

revision = "b18a04f8c019"
down_revision = "b17a93f7b018"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "exam_visual_selections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Uuid(), sa.ForeignKey("exam_grading_workspaces.id"), nullable=False
        ),
        sa.Column(
            "script_version_id",
            sa.Uuid(),
            sa.ForeignKey("answer_script_versions.id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("text_answer_review_id", sa.Uuid(), sa.ForeignKey("answer_review_decisions.id")),
        sa.Column("regions_sha256", sa.String(64), nullable=False),
        sa.Column(
            "creation_txid", sa.BigInteger(), nullable=False, server_default=sa.func.txid_current()
        ),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("workspace_id", "revision", name="uq_exam_visual_selection_revision"),
        sa.UniqueConstraint("id", "script_version_id", name="uq_exam_visual_selection_version"),
        sa.CheckConstraint("revision BETWEEN 1 AND 100", name="ck_exam_visual_selection_revision"),
        sa.CheckConstraint("length(regions_sha256)=64", name="ck_exam_visual_selection_sha"),
        sa.CheckConstraint(
            "NOT active OR text_answer_review_id IS NULL", name="ck_exam_visual_selection_mode"
        ),
    )
    op.create_table(
        "exam_visual_regions",
        sa.Column("selection_id", sa.Uuid(), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("script_version_id", sa.Uuid(), nullable=False),
        sa.Column("scan_page_id", sa.Uuid(), nullable=False),
        sa.Column("page_sha256", sa.String(64), nullable=False),
        sa.Column("crop_sha256", sa.String(64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("x0", sa.Integer(), nullable=False),
        sa.Column("y0", sa.Integer(), nullable=False),
        sa.Column("x1", sa.Integer(), nullable=False),
        sa.Column("y1", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["selection_id", "script_version_id"],
            ["exam_visual_selections.id", "exam_visual_selections.script_version_id"],
            name="fk_exam_visual_region_selection",
        ),
        sa.ForeignKeyConstraint(
            ["script_version_id", "scan_page_id"],
            ["answer_script_pages.version_id", "answer_script_pages.scan_page_id"],
            name="fk_exam_visual_region_page",
        ),
        sa.CheckConstraint("ordinal BETWEEN 0 AND 7", name="ck_exam_visual_region_ordinal"),
        sa.CheckConstraint(
            "width BETWEEN 1 AND 4096 AND height BETWEEN 1 AND 4096 AND x0>=0 AND y0>=0 AND x1>x0 AND y1>y0 AND x1<=width AND y1<=height",
            name="ck_exam_visual_region_bounds",
        ),
        sa.CheckConstraint(
            "length(page_sha256)=64 AND length(crop_sha256)=64", name="ck_exam_visual_region_sha"
        ),
    )
    op.alter_column(
        "grade_input_artifacts", "review_decision_id", existing_type=sa.Uuid(), nullable=True
    )
    op.add_column(
        "grade_input_artifacts", sa.Column("visual_selection_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        None,
        "grade_input_artifacts",
        "exam_visual_selections",
        ["visual_selection_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_grade_inputs_source_mode",
        "grade_input_artifacts",
        "(schema_version='visual-1.0' AND visual_selection_id IS NOT NULL AND review_decision_id IS NULL) OR (schema_version<>'visual-1.0' AND visual_selection_id IS NULL AND review_decision_id IS NOT NULL)",
    )
    op.alter_column("exam_scoring_jobs", "answer_review_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("exam_scoring_jobs", sa.Column("visual_selection_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        None, "exam_scoring_jobs", "exam_visual_selections", ["visual_selection_id"], ["id"]
    )
    op.create_unique_constraint(
        "uq_exam_visual_score", "exam_scoring_jobs", ["workspace_id", "visual_selection_id"]
    )
    op.create_check_constraint(
        "ck_exam_score_source_mode",
        "exam_scoring_jobs",
        "(answer_review_id IS NOT NULL AND visual_selection_id IS NULL) OR (answer_review_id IS NULL AND visual_selection_id IS NOT NULL)",
    )
    op.drop_constraint("ck_grade_review_metadata", "grade_review_revisions", type_="check")
    op.create_check_constraint(
        "ck_grade_review_metadata",
        "grade_review_revisions",
        "review_schema_version IN ('1.0','2.0') AND length(trim(reviewer_ref)) > 0 AND length(proposal_sha256) = 64 AND length(grade_input_sha256) = 64 AND length(request_sha256) = 64",
    )
    for statement in SELECTION_GUARDS.strip().split(";\nCREATE FUNCTION"):
        op.execute(
            statement if statement.startswith("CREATE FUNCTION") else "CREATE FUNCTION" + statement
        )
    op.execute(SCORE_GUARD)
    op.execute(
        "CREATE TRIGGER protect_visual_selection BEFORE INSERT OR UPDATE OR DELETE ON exam_visual_selections FOR EACH ROW EXECUTE FUNCTION guard_visual_selection()"
    )
    op.execute(
        "CREATE TRIGGER protect_visual_region BEFORE INSERT OR UPDATE OR DELETE ON exam_visual_regions FOR EACH ROW EXECUTE FUNCTION guard_visual_region()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER visual_selection_complete AFTER INSERT ON exam_visual_selections DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_visual_selection_complete()"
    )
    op.execute(
        "CREATE TRIGGER visual_review_mode BEFORE INSERT ON grade_review_revisions FOR EACH ROW EXECUTE FUNCTION guard_visual_review_mode()"
    )


SELECTION_GUARDS = r"""
CREATE FUNCTION guard_visual_selection() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE next_revision integer;
BEGIN
 IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'visual_selection_immutable' USING ERRCODE='23514'; END IF;
 PERFORM 1 FROM exam_grading_workspaces WHERE id=NEW.workspace_id FOR UPDATE;
 SELECT coalesce(max(revision),0)+1 INTO next_revision FROM exam_visual_selections WHERE workspace_id=NEW.workspace_id;
 IF NEW.revision<>next_revision OR NOT EXISTS (
  SELECT 1 FROM exam_grading_workspaces w JOIN exam_parsing_jobs j ON j.id=w.parsing_job_id
  JOIN answer_scripts root ON root.id=j.answer_script_id
  JOIN answer_script_versions v ON v.id=j.answer_script_version_id
  WHERE w.id=NEW.workspace_id AND j.status='succeeded' AND v.id=NEW.script_version_id
    AND root.current_version_id=v.id AND v.status='confirmed' AND v.completeness='complete'
 ) THEN RAISE EXCEPTION 'visual_selection_source_invalid' USING ERRCODE='23514'; END IF;
 IF NEW.text_answer_review_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM answer_review_decisions ar JOIN exam_submission_links k ON k.submission_id=ar.submission_id
  JOIN exam_grading_workspaces w ON w.parsing_job_id=k.job_id AND w.question_id=ar.question_id
  WHERE ar.id=NEW.text_answer_review_id AND w.id=NEW.workspace_id
    AND k.answer_script_version_id=NEW.script_version_id
 ) THEN RAISE EXCEPTION 'visual_selection_text_review_invalid' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION guard_visual_region() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'visual_region_immutable' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM exam_visual_selections s JOIN answer_script_pages p
  ON p.version_id=s.script_version_id AND p.scan_page_id=NEW.scan_page_id
  WHERE s.id=NEW.selection_id AND s.active AND s.creation_txid=txid_current()
    AND p.page_sha256=NEW.page_sha256)
 THEN RAISE EXCEPTION 'visual_region_source_invalid' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION check_visual_selection_complete() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE n integer; max_order integer;
BEGIN
 SELECT count(*),max(ordinal) INTO n,max_order FROM exam_visual_regions WHERE selection_id=NEW.id;
 IF (NEW.active AND (n<1 OR n>8 OR max_order<>n-1)) OR (NOT NEW.active AND n<>0)
 THEN RAISE EXCEPTION 'visual_selection_incomplete' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION guard_visual_review_mode() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM grade_input_artifacts gi WHERE gi.id=NEW.grade_input_artifact_id
 AND ((gi.schema_version='visual-1.0' AND NEW.review_schema_version='2.0')
 OR (gi.schema_version<>'visual-1.0' AND NEW.review_schema_version='1.0')))
 THEN RAISE EXCEPTION 'visual_review_mode_mismatch' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
"""


SCORE_GUARD = r"""
CREATE OR REPLACE FUNCTION guard_exam_scoring_job() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'exam_score_history_immutable' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.status<>'pending' THEN RAISE EXCEPTION 'exam_score_initial_state' USING ERRCODE='23514'; END IF;
  IF NEW.visual_selection_id IS NULL THEN
   IF NOT EXISTS(SELECT 1 FROM exam_grading_workspaces w JOIN exam_submission_links k ON k.job_id=w.parsing_job_id
    JOIN grade_input_artifacts gi ON gi.id=NEW.grade_input_id JOIN answer_review_decisions ar ON ar.id=NEW.answer_review_id
    WHERE w.id=NEW.workspace_id AND gi.submission_id=k.submission_id AND gi.question_id=w.question_id
    AND gi.review_decision_id=ar.id AND gi.alignment_artifact_id=w.alignment_artifact_id
    AND ar.submission_id=k.submission_id AND ar.question_id=w.question_id)
   THEN RAISE EXCEPTION 'exam_score_input_mismatch' USING ERRCODE='23514'; END IF;
  ELSE
   IF NOT EXISTS(SELECT 1 FROM exam_grading_workspaces w JOIN exam_submission_links k ON k.job_id=w.parsing_job_id
    JOIN grade_input_artifacts gi ON gi.id=NEW.grade_input_id
    JOIN exam_visual_selections v ON v.id=NEW.visual_selection_id
    WHERE w.id=NEW.workspace_id AND v.workspace_id=w.id AND v.active
    AND v.script_version_id=k.answer_script_version_id AND gi.visual_selection_id=v.id
    AND gi.schema_version='visual-1.0' AND gi.submission_id=k.submission_id
    AND gi.question_id=w.question_id AND gi.alignment_artifact_id=w.alignment_artifact_id
    AND gi.review_revision=v.revision AND gi.review_request_sha256=v.regions_sha256)
   THEN RAISE EXCEPTION 'exam_visual_score_input_mismatch' USING ERRCODE='23514'; END IF;
  END IF;
 ELSE
  IF OLD.status IN ('succeeded','failed','invalidated') OR NEW.id<>OLD.id OR NEW.workspace_id<>OLD.workspace_id
   OR NEW.answer_review_id IS DISTINCT FROM OLD.answer_review_id OR NEW.visual_selection_id IS DISTINCT FROM OLD.visual_selection_id
   OR NEW.grade_input_id<>OLD.grade_input_id OR NEW.requested_by_user_id<>OLD.requested_by_user_id OR NEW.created_at<>OLD.created_at
  THEN RAISE EXCEPTION 'exam_score_identity_immutable' USING ERRCODE='23514'; END IF;
  IF NEW.status='running' AND OLD.status='running' AND (OLD.lease_token IS DISTINCT FROM NEW.lease_token OR OLD.lease_expires_at<=clock_timestamp())
  THEN RAISE EXCEPTION 'exam_score_lease_lost' USING ERRCODE='23514'; END IF;
  IF NEW.status='succeeded' AND (OLD.status<>'running' OR OLD.lease_expires_at<=clock_timestamp())
  THEN RAISE EXCEPTION 'exam_score_lease_lost' USING ERRCODE='23514'; END IF;
 END IF;
 IF NEW.status='succeeded' AND NOT EXISTS(SELECT 1 FROM grade_proposal_artifacts gp JOIN grade_input_artifacts gi ON gi.id=NEW.grade_input_id
  WHERE gp.id=NEW.proposal_id AND gp.grade_input_artifact_id=gi.id AND gp.grade_input_sha256=gi.input_sha256
  AND gp.submission_id=gi.submission_id AND gp.question_id=gi.question_id)
 THEN RAISE EXCEPTION 'exam_score_result_mismatch' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
"""


def downgrade():
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM exam_visual_selections) OR EXISTS(SELECT 1 FROM grade_input_artifacts WHERE visual_selection_id IS NOT NULL) OR EXISTS(SELECT 1 FROM grade_review_revisions WHERE review_schema_version='2.0')"
        )
    ).scalar():
        raise RuntimeError("存在图像评分历史，禁止破坏性降级")
    op.execute("DROP TRIGGER visual_review_mode ON grade_review_revisions")
    op.execute("DROP TRIGGER visual_selection_complete ON exam_visual_selections")
    op.execute("DROP TRIGGER protect_visual_region ON exam_visual_regions")
    op.execute("DROP TRIGGER protect_visual_selection ON exam_visual_selections")
    for fn in (
        "guard_visual_review_mode",
        "check_visual_selection_complete",
        "guard_visual_region",
        "guard_visual_selection",
    ):
        op.execute(f"DROP FUNCTION {fn}()")
    op.drop_constraint("ck_grade_review_metadata", "grade_review_revisions", type_="check")
    op.create_check_constraint(
        "ck_grade_review_metadata",
        "grade_review_revisions",
        "review_schema_version = '1.0' AND length(trim(reviewer_ref)) > 0 AND length(proposal_sha256) = 64 AND length(grade_input_sha256) = 64 AND length(request_sha256) = 64",
    )
    op.drop_constraint("ck_exam_score_source_mode", "exam_scoring_jobs", type_="check")
    op.drop_constraint("uq_exam_visual_score", "exam_scoring_jobs", type_="unique")
    op.drop_constraint(
        "exam_scoring_jobs_visual_selection_id_fkey", "exam_scoring_jobs", type_="foreignkey"
    )
    op.drop_column("exam_scoring_jobs", "visual_selection_id")
    op.alter_column(
        "exam_scoring_jobs", "answer_review_id", existing_type=sa.Uuid(), nullable=False
    )
    op.drop_constraint("ck_grade_inputs_source_mode", "grade_input_artifacts", type_="check")
    op.drop_constraint(
        "grade_input_artifacts_visual_selection_id_fkey",
        "grade_input_artifacts",
        type_="foreignkey",
    )
    op.drop_column("grade_input_artifacts", "visual_selection_id")
    op.alter_column(
        "grade_input_artifacts", "review_decision_id", existing_type=sa.Uuid(), nullable=False
    )
    # Restore exactly the previous scoring guard from the historical migration.
    import importlib

    old = importlib.import_module(
        "migrations.versions.b14c52b3f014_考试作答复核与异步候选评分工作区"
    )
    score = (
        "CREATE FUNCTION guard_exam_scoring_job()"
        + old.GUARDS.split("CREATE FUNCTION guard_exam_scoring_job()", 1)[1]
    )
    op.execute(score.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
    op.drop_table("exam_visual_regions")
    op.drop_table("exam_visual_selections")
