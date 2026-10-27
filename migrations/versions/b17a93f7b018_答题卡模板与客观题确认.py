"""Immutable OMR forms and teacher-confirmed objective results."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "b17a93f7b018"
down_revision = "b16a82e6a017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_mapping_id_paper", "paper_question_mappings", ["id", "paper_version_id"]
    )
    op.create_table(
        "omr_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "paper_version_id", sa.Uuid(), sa.ForeignKey("paper_versions.id"), nullable=False
        ),
        sa.Column("engine_version", sa.String(40), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("mapping_digest", sa.String(64), nullable=False),
        sa.Column(
            "creation_txid", sa.BigInteger(), nullable=False, server_default=sa.func.txid_current()
        ),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("paper_version_id", name="uq_omr_paper_template"),
        sa.UniqueConstraint("id", "paper_version_id", name="uq_omr_template_paper"),
        sa.CheckConstraint(
            "length(content_sha256)=64 AND length(mapping_digest)=64", name="ck_omr_template_digest"
        ),
    )
    op.create_table(
        "omr_template_questions",
        sa.Column("template_id", sa.Uuid(), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("paper_version_id", sa.Uuid(), nullable=False),
        sa.Column("mapping_id", sa.Uuid(), nullable=False),
        sa.Column("printed_label", sa.String(12), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id", "paper_version_id"],
            ["omr_templates.id", "omr_templates.paper_version_id"],
            name="fk_omr_question_template",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_id", "paper_version_id"],
            ["paper_question_mappings.id", "paper_question_mappings.paper_version_id"],
            name="fk_omr_question_paper",
        ),
        sa.UniqueConstraint("template_id", "mapping_id", name="uq_omr_template_mapping"),
        sa.CheckConstraint("ordinal BETWEEN 0 AND 29", name="ck_omr_question_order"),
    )
    op.create_table(
        "omr_recognitions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("paper_version_id", sa.Uuid(), nullable=False),
        sa.Column("script_id", sa.Uuid(), nullable=False),
        sa.Column("script_version_id", sa.Uuid(), nullable=False),
        sa.Column("exam_session_id", sa.Uuid(), nullable=False),
        sa.Column("scan_page_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("page_sha256", sa.String(64), nullable=False),
        sa.Column("results", JSONB(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["template_id", "paper_version_id"],
            ["omr_templates.id", "omr_templates.paper_version_id"],
            name="fk_omr_recognition_template",
        ),
        sa.ForeignKeyConstraint(
            ["script_version_id", "script_id", "exam_session_id"],
            [
                "answer_script_versions.id",
                "answer_script_versions.answer_script_id",
                "answer_script_versions.exam_session_id",
            ],
            name="fk_omr_recognition_script",
        ),
        sa.ForeignKeyConstraint(
            ["script_version_id", "scan_page_id"],
            ["answer_script_pages.version_id", "answer_script_pages.scan_page_id"],
            name="fk_omr_recognition_page",
        ),
        sa.UniqueConstraint("script_version_id", "template_id", name="uq_omr_source_template"),
        sa.UniqueConstraint("id", "template_id", name="uq_omr_recognition_template"),
        sa.CheckConstraint(
            "length(manifest_sha256)=64 AND length(page_sha256)=64",
            name="ck_omr_recognition_digest",
        ),
        sa.CheckConstraint("jsonb_typeof(results)='array'", name="ck_omr_recognition_results"),
    )
    op.create_table(
        "omr_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("recognition_id", sa.Uuid(), nullable=False),
        sa.Column("template_id", sa.Uuid(), nullable=False),
        sa.Column("mapping_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("choice", sa.String(8), nullable=False),
        sa.Column("points", sa.Numeric(8, 2), nullable=False),
        sa.Column("reason", sa.String(300), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["recognition_id", "template_id"],
            ["omr_recognitions.id", "omr_recognitions.template_id"],
            name="fk_omr_decision_recognition",
        ),
        sa.ForeignKeyConstraint(
            ["template_id", "mapping_id"],
            ["omr_template_questions.template_id", "omr_template_questions.mapping_id"],
            name="fk_omr_decision_question",
        ),
        sa.UniqueConstraint(
            "recognition_id", "mapping_id", "revision", name="uq_omr_decision_revision"
        ),
        sa.CheckConstraint(
            "revision>0 AND choice IN ('A','B','C','D','blank') AND points>=0",
            name="ck_omr_decision_value",
        ),
        sa.CheckConstraint("length(trim(reason)) BETWEEN 1 AND 300", name="ck_omr_decision_reason"),
    )
    op.create_table(
        "omr_operations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("actor_user_id", "request_id", name="uq_omr_operation_request"),
        sa.CheckConstraint(
            "length(payload_sha256)=64 AND jsonb_typeof(result)='object'",
            name="ck_omr_operation_digest",
        ),
    )
    for table in [
        "omr_templates",
        "omr_template_questions",
        "omr_recognitions",
        "omr_decisions",
        "omr_operations",
    ]:
        op.execute(
            f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION guard_security_immutable()"
        )
    op.execute("""
    CREATE FUNCTION guard_omr_recognition_source() RETURNS trigger AS $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM answer_script_versions v
        JOIN session_papers sp ON sp.id=v.session_paper_id
        JOIN answer_script_pages pg ON pg.version_id=v.id AND pg.scan_page_id=NEW.scan_page_id
        JOIN answer_scripts root ON root.id=v.answer_script_id AND root.current_version_id=v.id
        JOIN paper_versions pv ON pv.id=sp.paper_version_id
        WHERE v.id=NEW.script_version_id AND v.status='confirmed' AND v.completeness='complete'
          AND v.exam_session_id=NEW.exam_session_id AND sp.paper_version_id=NEW.paper_version_id
          AND sp.status='active' AND pv.status='locked'
          AND v.manifest_sha256=NEW.manifest_sha256 AND pg.page_sha256=NEW.page_sha256
      ) THEN RAISE EXCEPTION 'invalid omr source' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER omr_source_guard BEFORE INSERT ON omr_recognitions "
        "FOR EACH ROW EXECUTE FUNCTION guard_omr_recognition_source()"
    )
    op.execute("""
    CREATE FUNCTION guard_omr_template_question() RETURNS trigger AS $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM omr_templates t
        JOIN paper_versions pv ON pv.id=t.paper_version_id
        JOIN paper_question_mappings m ON m.id=NEW.mapping_id AND m.paper_version_id=pv.id
        JOIN questions q ON q.id=m.legacy_question_id
        JOIN answer_keys k ON k.question_id=q.id
        WHERE t.id=NEW.template_id AND t.creation_txid=txid_current()
          AND pv.status='locked' AND t.content_sha256=pv.content_sha256
          AND t.mapping_digest=pv.mapping_digest AND m.printed_label=NEW.printed_label
          AND q.question_type='single_choice' AND k.source='teacher'
          AND trim(k.answer_content) IN ('A','B','C','D')
          AND (SELECT sum(points) FROM rubric_criteria WHERE question_id=q.id)=q.max_score
      ) THEN RAISE EXCEPTION 'invalid immutable omr template' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER omr_template_question_guard BEFORE INSERT ON omr_template_questions "
        "FOR EACH ROW EXECUTE FUNCTION guard_omr_template_question()"
    )
    op.execute("""
    CREATE FUNCTION check_omr_template_complete() RETURNS trigger AS $$
    DECLARE n integer; top_order integer;
    BEGIN
      SELECT count(*), max(ordinal) INTO n, top_order FROM omr_template_questions
        WHERE template_id=NEW.id;
      IF n<1 OR n>30 OR top_order<>n-1 THEN
        RAISE EXCEPTION 'invalid omr template questions' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE CONSTRAINT TRIGGER omr_template_complete AFTER INSERT ON omr_templates "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_omr_template_complete()"
    )
    op.execute("""
    CREATE FUNCTION guard_omr_decision() RETURNS trigger AS $$
    DECLARE expected numeric; next_revision integer;
    BEGIN
      SELECT CASE WHEN NEW.choice=trim(k.answer_content) THEN q.max_score ELSE 0 END
        INTO expected FROM omr_template_questions tq
        JOIN paper_question_mappings m ON m.id=tq.mapping_id
        JOIN questions q ON q.id=m.legacy_question_id
        JOIN answer_keys k ON k.question_id=q.id
        WHERE tq.template_id=NEW.template_id AND tq.mapping_id=NEW.mapping_id;
      SELECT coalesce(max(revision),0)+1 INTO next_revision FROM omr_decisions
        WHERE recognition_id=NEW.recognition_id AND mapping_id=NEW.mapping_id;
      IF expected IS NULL OR NEW.points<>expected OR NEW.revision<>next_revision THEN
        RAISE EXCEPTION 'invalid omr score or revision' USING ERRCODE='23514';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER omr_decision_guard BEFORE INSERT ON omr_decisions "
        "FOR EACH ROW EXECUTE FUNCTION guard_omr_decision()"
    )


def downgrade():
    bind = op.get_bind()
    for table in ["omr_templates", "omr_recognitions", "omr_decisions", "omr_operations"]:
        if bind.execute(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("OMR 业务事实存在，禁止破坏性降级")
    op.execute("DROP TRIGGER omr_source_guard ON omr_recognitions")
    op.execute("DROP FUNCTION guard_omr_recognition_source()")
    op.execute("DROP TRIGGER omr_decision_guard ON omr_decisions")
    op.execute("DROP FUNCTION guard_omr_decision()")
    op.execute("DROP TRIGGER omr_template_question_guard ON omr_template_questions")
    op.execute("DROP FUNCTION guard_omr_template_question()")
    op.execute("DROP TRIGGER omr_template_complete ON omr_templates")
    op.execute("DROP FUNCTION check_omr_template_complete()")
    for table in [
        "omr_operations",
        "omr_decisions",
        "omr_recognitions",
        "omr_template_questions",
        "omr_templates",
    ]:
        op.drop_table(table)
    op.drop_constraint("uq_mapping_id_paper", "paper_question_mappings", type_="unique")
