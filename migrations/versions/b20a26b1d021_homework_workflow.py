"""Trusted teaching relationships and immutable homework attempt history."""
# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op

revision = "b20a26b1d021"
down_revision = "b19a15a0c020"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "homework_teaching_assignments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "teacher_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "class_id",
            sa.Uuid(),
            sa.ForeignKey("administrative_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "term_id",
            sa.Uuid(),
            sa.ForeignKey("academic_terms.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.Uuid(),
            sa.ForeignKey("subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "granted_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT")),
    )
    op.create_index(
        "uq_homework_teaching_active",
        "homework_teaching_assignments",
        ["teacher_id", "class_id", "term_id", "subject_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "homework_offerings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "assignment_id",
            sa.Uuid(),
            sa.ForeignKey("assignments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "term_id",
            sa.Uuid(),
            sa.ForeignKey("academic_terms.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.Uuid(),
            sa.ForeignKey("subjects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("assignment_id", name="uq_homework_offering_assignment"),
    )
    op.create_table(
        "homework_classes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "offering_id",
            sa.Uuid(),
            sa.ForeignKey("homework_offerings.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "class_id",
            sa.Uuid(),
            sa.ForeignKey("administrative_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("class_name", sa.String(100), nullable=False),
        sa.UniqueConstraint("offering_id", "class_id", name="uq_homework_offering_class"),
    )
    op.create_table(
        "homework_participants",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "offering_id",
            sa.Uuid(),
            sa.ForeignKey("homework_offerings.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "student_id",
            sa.Uuid(),
            sa.ForeignKey("students.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "enrollment_id",
            sa.Uuid(),
            sa.ForeignKey("class_enrollments.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "class_id",
            sa.Uuid(),
            sa.ForeignKey("administrative_classes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("student_name", sa.String(100), nullable=False),
        sa.Column("student_code", sa.String(64), nullable=False),
        sa.UniqueConstraint("offering_id", "student_id", name="uq_homework_offering_student"),
        sa.UniqueConstraint("id", "offering_id", name="uq_homework_participant_offering"),
        sa.ForeignKeyConstraint(
            ["offering_id", "class_id"],
            ["homework_classes.offering_id", "homework_classes.class_id"],
            name="fk_homework_participant_class",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "homework_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "participant_id",
            sa.Uuid(),
            sa.ForeignKey("homework_participants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "submission_id",
            sa.Uuid(),
            sa.ForeignKey("submissions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column(
            "submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("participant_id", "version", name="uq_homework_attempt_version"),
        sa.UniqueConstraint("actor_id", "request_id", name="uq_homework_attempt_request"),
        sa.UniqueConstraint("submission_id", name="uq_homework_attempt_submission"),
        sa.CheckConstraint(
            "version > 0 AND length(file_sha256) = 64", name="ck_homework_attempt_data"
        ),
    )
    op.create_index(
        "uq_homework_attempt_current",
        "homework_attempts",
        ["participant_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.execute("""
    CREATE FUNCTION guard_homework_participant() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'homework_participant_immutable' USING ERRCODE='23514'; END IF;
      IF NOT EXISTS (SELECT 1 FROM class_enrollments e JOIN students s ON s.id=e.student_id
        JOIN homework_offerings o ON o.id=NEW.offering_id
        WHERE e.id=NEW.enrollment_id AND e.student_id=NEW.student_id
        AND e.administrative_class_id=NEW.class_id AND s.is_active
        AND e.valid_from <= (o.created_at AT TIME ZONE 'Asia/Shanghai')::date
        AND (e.valid_to IS NULL OR e.valid_to > (o.created_at AT TIME ZONE 'Asia/Shanghai')::date)
        AND s.name=NEW.student_name AND s.school_student_code=NEW.student_code)
      THEN RAISE EXCEPTION 'homework_enrollment_mismatch' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute(
        "CREATE TRIGGER guard_homework_participant BEFORE INSERT OR UPDATE OR DELETE ON homework_participants FOR EACH ROW EXECUTE FUNCTION guard_homework_participant()"
    )
    op.execute("""
    CREATE FUNCTION guard_homework_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'homework_attempt_immutable' USING ERRCODE='23514'; END IF;
      IF TG_OP = 'UPDATE' THEN
        IF NOT OLD.is_current OR NEW.is_current OR (NEW.id,NEW.participant_id,NEW.submission_id,NEW.version,NEW.actor_id,NEW.request_id,NEW.file_sha256,NEW.submitted_at) IS DISTINCT FROM (OLD.id,OLD.participant_id,OLD.submission_id,OLD.version,OLD.actor_id,OLD.request_id,OLD.file_sha256,OLD.submitted_at)
        THEN RAISE EXCEPTION 'homework_attempt_immutable' USING ERRCODE='23514'; END IF;
      ELSE
        IF NOT NEW.is_current OR NOT EXISTS (SELECT 1 FROM homework_participants p JOIN homework_offerings o ON o.id=p.offering_id JOIN submissions s ON s.id=NEW.submission_id JOIN submission_files f ON f.submission_id=s.id WHERE p.id=NEW.participant_id AND s.assignment_id=o.assignment_id AND f.sha256=NEW.file_sha256)
        THEN RAISE EXCEPTION 'homework_attempt_source_mismatch' USING ERRCODE='23514'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    """)
    op.execute(
        "CREATE TRIGGER guard_homework_attempt BEFORE INSERT OR UPDATE OR DELETE ON homework_attempts FOR EACH ROW EXECUTE FUNCTION guard_homework_attempt()"
    )


def downgrade():
    conn = op.get_bind()
    if any(
        conn.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar()
        for table in ("homework_teaching_assignments", "homework_offerings", "homework_attempts")
    ):
        raise RuntimeError("存在任教关系或作业记录，禁止破坏性降级")
    op.execute("DROP TRIGGER guard_homework_attempt ON homework_attempts")
    op.execute("DROP TRIGGER guard_homework_participant ON homework_participants")
    op.execute("DROP FUNCTION guard_homework_attempt()")
    op.execute("DROP FUNCTION guard_homework_participant()")
    for table in (
        "homework_attempts",
        "homework_participants",
        "homework_classes",
        "homework_offerings",
        "homework_teaching_assignments",
    ):
        op.drop_table(table)
