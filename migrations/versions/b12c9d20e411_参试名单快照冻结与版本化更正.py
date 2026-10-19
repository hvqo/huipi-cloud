"""参试名单快照冻结与版本化更正

Revision ID: b12c9d20e411
Revises: b12bf031a920
Create Date: 2026-10-19 22:09:05.282853

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b12c9d20e411"
down_revision: Union[str, Sequence[str], None] = "b12bf031a920"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint(
        "uq_session_paper_identity", "session_papers", ["id", "exam_session_id"]
    )

    op.create_table(
        "roster_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('create','replace','correct','freeze','enable_operator')",
            name="ck_roster_operation_action",
        ),
        sa.CheckConstraint("jsonb_typeof(result)='object'", name="ck_roster_operation_result"),
        sa.CheckConstraint("length(payload_sha256)=64", name="ck_roster_operation_digest"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_user_id", "request_id", name="uq_roster_operation_request"),
    )
    op.create_table(
        "exam_participations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exam_session_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["exam_session_id"], ["exam_sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "exam_session_id", "student_id", name="uq_participation_session_student"
        ),
        sa.UniqueConstraint(
            "id", "exam_session_id", "student_id", name="uq_participation_identity"
        ),
    )
    op.create_table(
        "roster_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("exam_session_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("previous_revision_id", sa.Uuid(), nullable=True),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="draft", nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("frozen_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status='draft' AND frozen_at IS NULL AND frozen_by_user_id IS NULL) OR "
            "(status='frozen' AND frozen_at IS NOT NULL AND frozen_by_user_id IS NOT NULL)",
            name="ck_roster_frozen",
        ),
        sa.CheckConstraint("NOT is_current OR status='frozen'", name="ck_roster_current"),
        sa.CheckConstraint("status IN ('draft','frozen')", name="ck_roster_status"),
        sa.CheckConstraint("length(trim(reason))>0", name="ck_roster_reason"),
        sa.CheckConstraint("version_no>0 AND revision>0", name="ck_roster_revision"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["exam_session_id"], ["exam_sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["frozen_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["previous_revision_id", "exam_session_id"],
            ["roster_revisions.id", "roster_revisions.exam_session_id"],
            name="fk_roster_previous_session",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exam_session_id", "version_no", name="uq_roster_version"),
        sa.UniqueConstraint("id", "exam_session_id", name="uq_roster_session"),
        sa.UniqueConstraint("previous_revision_id", name="uq_roster_successor"),
    )
    op.create_index(
        "uq_roster_current",
        "roster_revisions",
        ["exam_session_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        "uq_roster_draft",
        "roster_revisions",
        ["exam_session_id"],
        unique=True,
        postgresql_where=sa.text("status='draft'"),
    )
    op.create_table(
        "roster_entry_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("roster_revision_id", sa.Uuid(), nullable=False),
        sa.Column("exam_session_id", sa.Uuid(), nullable=False),
        sa.Column("participation_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("enrollment_id", sa.Uuid(), nullable=False),
        sa.Column("administrative_class_id", sa.Uuid(), nullable=False),
        sa.Column("student_name", sa.String(length=100), nullable=False),
        sa.Column("student_code", sa.String(length=64), nullable=False),
        sa.Column("class_name", sa.String(length=100), nullable=False),
        sa.Column("class_code", sa.String(length=32), nullable=False),
        sa.Column("exam_number", sa.String(length=32), nullable=True),
        sa.Column("session_paper_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "exam_number IS NULL OR exam_number ~ '^[A-Z0-9_-]{1,32}$'",
            name="ck_roster_exam_number",
        ),
        sa.CheckConstraint(
            "length(trim(student_name))>0 AND length(trim(student_code))>0 AND "
            "length(trim(class_name))>0 AND length(trim(class_code))>0",
            name="ck_roster_entry_labels",
        ),
        sa.ForeignKeyConstraint(
            ["administrative_class_id"], ["administrative_classes.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["enrollment_id"], ["class_enrollments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["participation_id", "exam_session_id", "student_id"],
            [
                "exam_participations.id",
                "exam_participations.exam_session_id",
                "exam_participations.student_id",
            ],
            name="fk_roster_entry_participation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["roster_revision_id", "exam_session_id"],
            ["roster_revisions.id", "roster_revisions.exam_session_id"],
            name="fk_roster_entry_revision",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_paper_id", "exam_session_id"],
            ["session_papers.id", "session_papers.exam_session_id"],
            name="fk_roster_entry_paper",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "roster_revision_id", "exam_number", name="uq_roster_entry_exam_number"
        ),
        sa.UniqueConstraint("roster_revision_id", "student_id", name="uq_roster_entry_student"),
    )
    op.create_index(
        "ix_roster_entry_participation",
        "roster_entry_snapshots",
        ["participation_id"],
        unique=False,
    )
    install_guards()
    seed_roles()


def downgrade() -> None:
    """Downgrade schema."""
    remove_guards_and_roles()
    op.drop_index("ix_roster_entry_participation", table_name="roster_entry_snapshots")
    op.drop_table("roster_entry_snapshots")
    op.drop_constraint("uq_session_paper_identity", "session_papers", type_="unique")
    op.drop_index(
        "uq_roster_draft", table_name="roster_revisions", postgresql_where=sa.text("status='draft'")
    )
    op.drop_index(
        "uq_roster_current", table_name="roster_revisions", postgresql_where=sa.text("is_current")
    )
    op.drop_table("roster_revisions")
    op.drop_table("exam_participations")
    op.drop_table("roster_operations")
    # ### end Alembic commands ###


TRIGGERS = """
CREATE FUNCTION roster_session_context(sid uuid) RETURNS exam_sessions LANGUAGE plpgsql AS $$
DECLARE s exam_sessions; state text;
BEGIN
 SELECT * INTO s FROM exam_sessions WHERE id=sid;
 IF NOT FOUND THEN RAISE EXCEPTION 'roster_session_missing' USING ERRCODE='23503'; END IF;
 SELECT lifecycle INTO state FROM exam_events WHERE id=s.exam_event_id FOR SHARE;
 SELECT * INTO s FROM exam_sessions WHERE id=sid FOR UPDATE;
 IF state NOT IN ('draft','active') OR s.lifecycle NOT IN ('draft','ready') THEN
  RAISE EXCEPTION 'roster_session_closed' USING ERRCODE='23514'; END IF;
 RETURN s;
END $$;

CREATE FUNCTION guard_roster_revision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s exam_sessions; parent roster_revisions; n integer; pid uuid;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'roster_history_immutable' USING ERRCODE='23514'; END IF;
 s:=roster_session_context(NEW.exam_session_id);
 IF TG_OP='INSERT' THEN
  IF NEW.status<>'draft' OR NEW.revision<>1 OR NEW.is_current OR NEW.frozen_at IS NOT NULL OR
     NEW.business_date<>(s.starts_at AT TIME ZONE 'Asia/Shanghai')::date THEN
   RAISE EXCEPTION 'roster_initial_state' USING ERRCODE='23514'; END IF;
  SELECT count(*) INTO n FROM roster_revisions WHERE exam_session_id=s.id;
  IF n>=100 OR NEW.version_no<>n+1 THEN
   RAISE EXCEPTION 'roster_version_conflict' USING ERRCODE='23514'; END IF;
  IF n=0 THEN
   IF NEW.previous_revision_id IS NOT NULL THEN RAISE EXCEPTION 'roster_parent_invalid' USING
ERRCODE='23514'; END IF;
  ELSE
   SELECT * INTO parent FROM roster_revisions WHERE id=NEW.previous_revision_id FOR SHARE;
   IF NOT FOUND OR parent.exam_session_id<>s.id OR parent.status<>'frozen' OR NOT
parent.is_current OR parent.business_date<>NEW.business_date THEN
    RAISE EXCEPTION 'roster_parent_invalid' USING ERRCODE='23514'; END IF;
  END IF;
  RETURN NEW;
 END IF;
 IF OLD.status='frozen' THEN
  IF NOT OLD.is_current OR NEW.is_current OR
to_jsonb(NEW)-'is_current'<>to_jsonb(OLD)-'is_current' THEN
   RAISE EXCEPTION 'roster_history_immutable' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF NEW.revision<>OLD.revision+1 OR
to_jsonb(NEW)-ARRAY['revision','status','is_current','frozen_at','frozen_by_user_id','updated_at']<>
to_jsonb(OLD)-ARRAY['revision','status','is_current','frozen_at','frozen_by_user_id','updated_at']
THEN
  RAISE EXCEPTION 'roster_identity_immutable' USING ERRCODE='23514'; END IF;
 IF NEW.status='frozen' THEN
  IF s.lifecycle<>'ready' OR NOT NEW.is_current THEN
   RAISE EXCEPTION 'roster_session_not_ready' USING ERRCODE='23514'; END IF;
  SELECT count(*) INTO n FROM roster_entry_snapshots WHERE roster_revision_id=NEW.id;
  IF n=0 OR n>2000 OR EXISTS(SELECT 1 FROM roster_entry_snapshots WHERE
roster_revision_id=NEW.id AND exam_number IS NULL) THEN
   RAISE EXCEPTION 'roster_incomplete' USING ERRCODE='23514'; END IF;
  -- Recheck current source identity and historical date relation. Labels remain captured facts.
  PERFORM 1 FROM students st JOIN roster_entry_snapshots e ON e.student_id=st.id
     WHERE e.roster_revision_id=NEW.id ORDER BY st.id FOR SHARE OF st;
  PERFORM 1 FROM class_enrollments ce JOIN roster_entry_snapshots e ON e.enrollment_id=ce.id
     WHERE e.roster_revision_id=NEW.id ORDER BY ce.id FOR SHARE OF ce;
  IF EXISTS(SELECT 1 FROM roster_entry_snapshots e JOIN students st ON st.id=e.student_id
      JOIN class_enrollments ce ON ce.id=e.enrollment_id WHERE e.roster_revision_id=NEW.id AND
      (NOT st.is_active OR ce.student_id<>e.student_id OR
ce.administrative_class_id<>e.administrative_class_id OR
       ce.valid_from>NEW.business_date OR (ce.valid_to IS NOT NULL AND
ce.valid_to<=NEW.business_date))) THEN
   RAISE EXCEPTION 'roster_source_changed' USING ERRCODE='23514'; END IF;
  FOR pid IN SELECT b.paper_version_id FROM session_papers b WHERE b.exam_session_id=s.id AND
b.status='active' ORDER BY b.paper_version_id LOOP
   PERFORM 1 FROM paper_versions WHERE id=pid AND status='locked' FOR SHARE;
   IF NOT FOUND THEN RAISE EXCEPTION 'roster_paper_unavailable' USING ERRCODE='23514'; END IF;
  END LOOP;
  IF EXISTS(SELECT 1 FROM roster_entry_snapshots e JOIN session_papers b ON b.id=e.session_paper_id
      WHERE e.roster_revision_id=NEW.id AND (b.exam_session_id<>s.id OR b.status<>'active')) THEN
   RAISE EXCEPTION 'roster_paper_invalid' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION guard_roster_entry() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r roster_revisions; st students; ce class_enrollments; cl administrative_classes; b
session_papers; rid uuid; sid uuid;
BEGIN
 IF TG_OP='UPDATE' THEN RAISE EXCEPTION 'roster_entry_replace_required' USING ERRCODE='23514';
END IF;
 IF TG_OP='DELETE' THEN rid:=OLD.roster_revision_id; sid:=OLD.exam_session_id;
 ELSE rid:=NEW.roster_revision_id; sid:=NEW.exam_session_id; END IF;
 PERFORM roster_session_context(sid);
 SELECT * INTO r FROM roster_revisions WHERE id=rid FOR UPDATE;
 IF NOT FOUND OR r.status<>'draft' OR r.exam_session_id<>sid THEN
  RAISE EXCEPTION 'roster_entry_frozen_or_invalid' USING ERRCODE='23514'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF (SELECT count(*) FROM roster_entry_snapshots WHERE roster_revision_id=rid)>=2000 THEN
  RAISE EXCEPTION 'roster_entry_limit' USING ERRCODE='23514'; END IF;
 SELECT * INTO st FROM students WHERE id=NEW.student_id FOR SHARE;
 SELECT * INTO ce FROM class_enrollments WHERE id=NEW.enrollment_id FOR SHARE;
 SELECT * INTO cl FROM administrative_classes WHERE id=NEW.administrative_class_id FOR SHARE;
 IF st.id IS NULL OR ce.id IS NULL OR cl.id IS NULL OR NOT st.is_active OR
    ce.student_id<>st.id OR ce.administrative_class_id<>cl.id OR ce.valid_from>r.business_date OR
    (ce.valid_to IS NOT NULL AND ce.valid_to<=r.business_date) OR
    NEW.student_name<>st.name OR NEW.student_code<>st.school_student_code OR
    NEW.class_name<>cl.display_name OR NEW.class_code<>cl.code THEN
  RAISE EXCEPTION 'roster_snapshot_invalid' USING ERRCODE='23514'; END IF;
 IF NEW.session_paper_id IS NOT NULL THEN
  SELECT * INTO b FROM session_papers WHERE id=NEW.session_paper_id FOR SHARE;
  IF NOT FOUND OR b.exam_session_id<>sid OR b.status<>'active' THEN
   RAISE EXCEPTION 'roster_paper_invalid' USING ERRCODE='23514'; END IF;
  PERFORM 1 FROM paper_versions WHERE id=b.paper_version_id AND status='locked' FOR SHARE;
  IF NOT FOUND THEN RAISE EXCEPTION 'roster_paper_unavailable' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION check_roster_current() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE highest integer; actual integer; n integer;
BEGIN
 SELECT max(version_no) INTO highest FROM roster_revisions WHERE
exam_session_id=NEW.exam_session_id AND status='frozen';
 SELECT count(*),max(version_no) INTO n,actual FROM roster_revisions WHERE
exam_session_id=NEW.exam_session_id AND is_current;
 IF highest IS NOT NULL AND (n<>1 OR actual IS DISTINCT FROM highest) THEN
  RAISE EXCEPTION 'roster_current_inconsistent' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;

CREATE FUNCTION guard_roster_session_schedule() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM roster_revisions WHERE exam_session_id=OLD.id) AND
    (NEW.starts_at<>OLD.starts_at OR NEW.ends_at<>OLD.ends_at OR
NEW.paper_policy<>OLD.paper_policy) THEN
  RAISE EXCEPTION 'roster_session_schedule_frozen' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
"""


def install_guards():
    for statement in TRIGGERS.split(";\n\n"):
        if statement.strip():
            op.execute(statement)
    for table, function, events in (
        ("roster_revisions", "guard_roster_revision", "INSERT OR UPDATE OR DELETE"),
        ("roster_entry_snapshots", "guard_roster_entry", "INSERT OR UPDATE OR DELETE"),
        ("exam_participations", "guard_security_immutable", "UPDATE OR DELETE"),
        ("roster_operations", "guard_security_immutable", "UPDATE OR DELETE"),
        ("exam_sessions", "guard_roster_session_schedule", "UPDATE"),
    ):
        op.execute(
            f"CREATE TRIGGER protect_roster BEFORE {events} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    op.execute(
        "CREATE CONSTRAINT TRIGGER roster_current_integrity AFTER INSERT OR UPDATE "
        "ON roster_revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION check_roster_current()"
    )


ACTIONS = ["exam.roster.read", "exam.roster.manage", "exam.roster.freeze", "exam.roster.correct"]
ROLE_CODES = [
    ("1", "roster_operator", ["security.grants.manage"] + ACTIONS),
    ("2", "roster_manager", ACTIONS),
    ("3", "roster_reader", ["exam.roster.read"]),
    ("4", "roster_freezer", ["exam.roster.read", "exam.roster.freeze"]),
]
ROLE_IDS = ["11111111-b12c-4000-8000-00000000000" + suffix for suffix, _, _ in ROLE_CODES]


def seed_roles():
    bind = op.get_bind()
    for action in ACTIONS:
        bind.execute(
            sa.text(
                "INSERT INTO permission_definitions VALUES (:code,true,"
                "ARRAY['installation','exam_event','exam_session'])"
            ),
            dict(code=action),
        )
    for identifier, (_, code, permissions) in zip(ROLE_IDS, ROLE_CODES, strict=True):
        bind.execute(
            sa.text("INSERT INTO security_role_versions VALUES (CAST(:id AS uuid),:code,1,NULL)"),
            dict(id=identifier, code=code),
        )
        for permission in permissions:
            bind.execute(
                sa.text("INSERT INTO security_role_permissions VALUES (CAST(:id AS uuid),:code)"),
                dict(id=identifier, code=permission),
            )
        bind.execute(
            sa.text(
                "UPDATE security_role_versions SET sealed_at=clock_timestamp() "
                "WHERE id=CAST(:id AS uuid)"
            ),
            dict(id=identifier),
        )


def remove_guards_and_roles():
    op.execute(
        "LOCK TABLE roster_revisions,roster_entry_snapshots,exam_participations,"
        "roster_operations,security_grants IN ACCESS EXCLUSIVE MODE"
    )
    bind = op.get_bind()
    used = any(
        bind.scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table})"))
        for table in (
            "roster_revisions",
            "roster_entry_snapshots",
            "exam_participations",
            "roster_operations",
        )
    )
    used = used or bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM security_grants WHERE "
            "role_version_id IN (SELECT unnest(CAST(:ids AS uuid[]))))"
        ),
        dict(ids=ROLE_IDS),
    )
    if used:
        raise RuntimeError("roster_downgrade_requires_archived_new_data")
    op.execute(
        "ALTER TABLE security_role_permissions DISABLE TRIGGER immutable_security_role_permissions"
    )
    op.execute("ALTER TABLE security_role_versions DISABLE TRIGGER immutable_security_role")
    for identifier in ROLE_IDS:
        for table, column in [
            ("security_role_permissions", "role_version_id"),
            ("security_role_versions", "id"),
        ]:
            bind.execute(
                sa.text(f"DELETE FROM {table} WHERE {column}=CAST(:id AS uuid)"),
                dict(id=identifier),
            )
    op.execute(
        "ALTER TABLE security_role_permissions ENABLE TRIGGER immutable_security_role_permissions"
    )
    op.execute("ALTER TABLE security_role_versions ENABLE TRIGGER immutable_security_role")
    for action in ACTIONS:
        bind.execute(
            sa.text("DELETE FROM permission_definitions WHERE code=:code"), dict(code=action)
        )
    op.execute("DROP TRIGGER roster_current_integrity ON roster_revisions")
    for table in (
        "roster_revisions",
        "roster_entry_snapshots",
        "exam_participations",
        "roster_operations",
        "exam_sessions",
    ):
        op.execute(f"DROP TRIGGER protect_roster ON {table}")
    for function in (
        "guard_roster_revision",
        "guard_roster_entry",
        "check_roster_current",
        "guard_roster_session_schedule",
    ):
        op.execute(f"DROP FUNCTION {function}()")
    op.execute("DROP FUNCTION roster_session_context(uuid)")
