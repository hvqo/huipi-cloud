"""Bind homework visual grading to real submission attempts and immutable selections."""
# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "b20b37c2e022"
down_revision = "b20a26b1d021"
branch_labels = None
depends_on = None

NEW_MODE = "(schema_version='visual-1.0' AND visual_selection_id IS NOT NULL AND homework_visual_selection_id IS NULL AND review_decision_id IS NULL) OR (schema_version='hw-visual-1' AND homework_visual_selection_id IS NOT NULL AND visual_selection_id IS NULL AND review_decision_id IS NULL) OR (schema_version NOT IN ('visual-1.0','hw-visual-1') AND visual_selection_id IS NULL AND homework_visual_selection_id IS NULL AND review_decision_id IS NOT NULL)"
OLD_MODE = "(schema_version='visual-1.0' AND visual_selection_id IS NOT NULL AND review_decision_id IS NULL) OR (schema_version<>'visual-1.0' AND visual_selection_id IS NULL AND review_decision_id IS NOT NULL)"


def review_guard(modes):
    return f"""
    CREATE OR REPLACE FUNCTION guard_visual_review_mode() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM grade_input_artifacts gi WHERE gi.id=NEW.grade_input_artifact_id
        AND ((gi.schema_version IN ({modes}) AND NEW.review_schema_version='2.0')
        OR (gi.schema_version NOT IN ({modes}) AND NEW.review_schema_version='1.0')))
      THEN RAISE EXCEPTION 'visual_review_mode_mismatch' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$;
    """


def upgrade():
    op.create_table(
        "homework_visual_selections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("homework_attempts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "submission_id",
            sa.Uuid(),
            sa.ForeignKey("submissions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey("submission_files.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "question_id",
            sa.Uuid(),
            sa.ForeignKey("questions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("regions", JSONB(), nullable=False),
        sa.Column("regions_sha256", sa.String(64), nullable=False),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "attempt_id", "question_id", "revision", name="uq_homework_visual_selection_revision"
        ),
        sa.UniqueConstraint(
            "actor_user_id", "request_id", name="uq_homework_visual_selection_request"
        ),
        sa.CheckConstraint(
            "revision BETWEEN 1 AND 100 AND length(regions_sha256)=64 AND length(request_sha256)=64",
            name="ck_homework_visual_selection_data",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(regions)='array' AND jsonb_array_length(regions) BETWEEN 1 AND 8",
            name="ck_homework_visual_selection_regions",
        ),
    )
    op.add_column(
        "grade_input_artifacts", sa.Column("homework_visual_selection_id", sa.Uuid(), nullable=True)
    )
    for column in (
        "canonical_artifact_id",
        "canonical_sha256",
        "alignment_artifact_id",
        "alignment_sha256",
    ):
        op.alter_column("grade_input_artifacts", column, nullable=True)
    op.create_check_constraint(
        "ck_grade_inputs_text_sources",
        "grade_input_artifacts",
        "(canonical_artifact_id IS NOT NULL AND canonical_sha256 IS NOT NULL AND alignment_artifact_id IS NOT NULL AND alignment_sha256 IS NOT NULL) OR (schema_version='hw-visual-1' AND canonical_artifact_id IS NULL AND canonical_sha256 IS NULL AND alignment_artifact_id IS NULL AND alignment_sha256 IS NULL)",
    )
    op.create_foreign_key(
        "fk_grade_input_homework_visual",
        "grade_input_artifacts",
        "homework_visual_selections",
        ["homework_visual_selection_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("ck_grade_inputs_source_mode", "grade_input_artifacts", type_="check")
    op.create_check_constraint("ck_grade_inputs_source_mode", "grade_input_artifacts", NEW_MODE)
    op.execute("""
    CREATE FUNCTION guard_homework_visual_input() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NEW.schema_version='hw-visual-1' AND NOT EXISTS (
        SELECT 1 FROM homework_visual_selections s
        JOIN homework_attempts a ON a.id=s.attempt_id
        JOIN submissions sub ON sub.id=s.submission_id
        WHERE s.id=NEW.homework_visual_selection_id AND a.is_current
          AND s.submission_id=NEW.submission_id AND s.question_id=NEW.question_id
          AND sub.assignment_id=NEW.assignment_id AND a.file_sha256=NEW.source_file_sha256
          AND s.revision=NEW.review_revision AND s.regions_sha256=NEW.review_request_sha256)
      THEN RAISE EXCEPTION 'homework_visual_input_source_invalid' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute(
        "CREATE TRIGGER guard_homework_visual_input BEFORE INSERT ON grade_input_artifacts FOR EACH ROW EXECUTE FUNCTION guard_homework_visual_input()"
    )
    op.create_table(
        "homework_visual_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "selection_id",
            sa.Uuid(),
            sa.ForeignKey("homework_visual_selections.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "proposal_id",
            sa.Uuid(),
            sa.ForeignKey("grade_proposal_artifacts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("actor_user_id", "request_id", name="uq_homework_visual_job_request"),
        sa.UniqueConstraint("selection_id", name="uq_homework_visual_job_selection"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed','invalidated')",
            name="ck_homework_visual_job_status",
        ),
        sa.CheckConstraint("length(request_sha256)=64", name="ck_homework_visual_job_digest"),
        sa.CheckConstraint(
            "(status='succeeded')=(proposal_id IS NOT NULL)", name="ck_homework_visual_job_result"
        ),
    )
    op.execute("""
    CREATE FUNCTION guard_homework_visual_selection() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'homework_visual_selection_immutable' USING ERRCODE='23514'; END IF;
      IF NOT EXISTS (SELECT 1 FROM homework_attempts a JOIN submissions s ON s.id=a.submission_id
        JOIN submission_files f ON f.id=NEW.file_id AND f.submission_id=s.id
        JOIN questions q ON q.id=NEW.question_id AND q.assignment_id=s.assignment_id
        WHERE a.id=NEW.attempt_id AND a.is_current AND s.id=NEW.submission_id AND a.file_sha256=f.sha256)
      THEN RAISE EXCEPTION 'homework_visual_selection_source_invalid' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute(
        "CREATE TRIGGER guard_homework_visual_selection BEFORE INSERT OR UPDATE OR DELETE ON homework_visual_selections FOR EACH ROW EXECUTE FUNCTION guard_homework_visual_selection()"
    )
    op.execute("""
    CREATE FUNCTION guard_homework_visual_job() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'homework_visual_job_immutable' USING ERRCODE='23514'; END IF;
      IF TG_OP='INSERT' AND NEW.status<>'running' THEN RAISE EXCEPTION 'homework_visual_job_state' USING ERRCODE='23514'; END IF;
      IF TG_OP='UPDATE' AND (OLD.status<>'running' OR NEW.status NOT IN ('succeeded','failed','invalidated')
        OR (NEW.id,NEW.selection_id,NEW.actor_user_id,NEW.request_id,NEW.request_sha256,NEW.created_at)
        IS DISTINCT FROM (OLD.id,OLD.selection_id,OLD.actor_user_id,OLD.request_id,OLD.request_sha256,OLD.created_at))
      THEN RAISE EXCEPTION 'homework_visual_job_immutable' USING ERRCODE='23514'; END IF;
      IF NEW.status='succeeded' AND NOT EXISTS (SELECT 1 FROM grade_proposal_artifacts gp
        JOIN grade_input_artifacts gi ON gi.id=gp.grade_input_artifact_id
        JOIN homework_visual_selections s ON s.id=NEW.selection_id
        WHERE gp.id=NEW.proposal_id AND gi.homework_visual_selection_id=s.id
        AND gi.submission_id=s.submission_id AND gi.question_id=s.question_id)
      THEN RAISE EXCEPTION 'homework_visual_job_proposal_mismatch' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute(
        "CREATE TRIGGER guard_homework_visual_job BEFORE INSERT OR UPDATE OR DELETE ON homework_visual_jobs FOR EACH ROW EXECUTE FUNCTION guard_homework_visual_job()"
    )
    op.execute(review_guard("'visual-1.0','hw-visual-1'"))


def downgrade():
    conn = op.get_bind()
    if conn.execute(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM homework_visual_selections) OR EXISTS(SELECT 1 FROM grade_input_artifacts WHERE homework_visual_selection_id IS NOT NULL)"
        )
    ).scalar():
        raise RuntimeError("存在日常作业视觉评分事实，禁止破坏性降级")
    op.execute(review_guard("'visual-1.0'"))
    op.execute("DROP TRIGGER guard_homework_visual_input ON grade_input_artifacts")
    op.execute("DROP FUNCTION guard_homework_visual_input()")
    op.drop_table("homework_visual_jobs")
    op.execute("DROP FUNCTION guard_homework_visual_job()")
    op.drop_constraint("ck_grade_inputs_source_mode", "grade_input_artifacts", type_="check")
    op.drop_constraint(
        "fk_grade_input_homework_visual", "grade_input_artifacts", type_="foreignkey"
    )
    op.drop_column("grade_input_artifacts", "homework_visual_selection_id")
    op.drop_constraint("ck_grade_inputs_text_sources", "grade_input_artifacts", type_="check")
    for column in (
        "canonical_artifact_id",
        "canonical_sha256",
        "alignment_artifact_id",
        "alignment_sha256",
    ):
        op.alter_column("grade_input_artifacts", column, nullable=False)
    op.create_check_constraint("ck_grade_inputs_source_mode", "grade_input_artifacts", OLD_MODE)
    op.drop_table("homework_visual_selections")
    op.execute("DROP FUNCTION guard_homework_visual_selection()")
