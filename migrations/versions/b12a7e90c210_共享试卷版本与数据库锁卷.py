"""
共享试卷版本与数据库锁卷

Revision ID: b12a7e90c210
Revises: b11b0a7c4e21
Create Date: 2026-10-18 23:44:17.207791

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b12a7e90c210"
down_revision: Union[str, Sequence[str], None] = "b11b0a7c4e21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_unique_constraint("uq_question_parent_pair", "questions", ["id", "assignment_id"])
    op.create_table(
        "paper_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN "
            "('snapshot','fork','edit_question','reorder','lock','retire','enable_operator')",
            name="ck_paper_operation_action",
        ),
        sa.CheckConstraint("jsonb_typeof(result)='object'", name="ck_paper_operation_result"),
        sa.CheckConstraint("length(payload_sha256)=64", name="ck_paper_operation_sha"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_user_id", "request_id", name="uq_paper_request"),
    )
    op.create_table(
        "paper_families",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("creation_key_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(creation_key_sha256)=64", name="ck_paper_family_digest"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("creation_key_sha256", name="uq_paper_family_creation"),
    )
    op.create_table(
        "paper_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("paper_family_id", sa.Uuid(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("canonical_assignment_id", sa.Uuid(), nullable=False),
        sa.Column("source_assignment_id", sa.Uuid(), nullable=False),
        sa.Column("prior_version_id", sa.Uuid(), nullable=True),
        sa.Column("source_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("question_digest", sa.String(length=64), nullable=False),
        sa.Column("rubric_digest", sa.String(length=64), nullable=False),
        sa.Column("mapping_digest", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status='draft' AND locked_at IS NULL) OR (status='locked' AND locked_at"
            " IS NOT NULL) OR status='retired'",
            name="ck_paper_lock_state",
        ),
        sa.CheckConstraint("status IN ('draft','locked','retired')", name="ck_paper_status"),
        sa.CheckConstraint(
            "length(source_content_sha256)=64 AND length(content_sha256)=64 AND "
            "length(question_digest)=64 AND length(rubric_digest)=64 AND "
            "length(mapping_digest)=64",
            name="ck_paper_digests",
        ),
        sa.CheckConstraint("length(trim(title))>0", name="ck_paper_title"),
        sa.CheckConstraint("version_no>0 AND revision>0", name="ck_paper_numbers"),
        sa.ForeignKeyConstraint(
            ["canonical_assignment_id"], ["assignments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["paper_family_id"],
            ["paper_families.id"],
        ),
        sa.ForeignKeyConstraint(
            ["prior_version_id", "paper_family_id"],
            ["paper_versions.id", "paper_versions.paper_family_id"],
            name="fk_paper_prior_family",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["source_assignment_id"], ["assignments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_assignment_id", name="uq_paper_content_carrier"),
        sa.UniqueConstraint("id", "canonical_assignment_id", name="uq_paper_carrier_pair"),
        sa.UniqueConstraint("id", "paper_family_id", name="uq_paper_family_pair"),
        sa.UniqueConstraint("paper_family_id", "version_no", name="uq_paper_family_version"),
        sa.UniqueConstraint("prior_version_id", name="uq_paper_linear_successor"),
    )
    op.create_table(
        "paper_question_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("paper_version_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_assignment_id", sa.Uuid(), nullable=False),
        sa.Column("question_key", sa.Uuid(), nullable=False),
        sa.Column("legacy_question_id", sa.Uuid(), nullable=False),
        sa.Column("printed_label", sa.String(length=80), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("source_question_id", sa.Uuid(), nullable=False),
        sa.Column("source_rubric_sha256", sa.String(length=64), nullable=False),
        sa.Column("prior_mapping_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("length(source_rubric_sha256)=64", name="ck_mapping_source_digest"),
        sa.CheckConstraint(
            "sort_order>0 AND length(trim(printed_label))>0", name="ck_mapping_order"
        ),
        sa.ForeignKeyConstraint(
            ["legacy_question_id", "canonical_assignment_id"],
            ["questions.id", "questions.assignment_id"],
            name="fk_mapping_question_carrier",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["paper_version_id", "canonical_assignment_id"],
            ["paper_versions.id", "paper_versions.canonical_assignment_id"],
            name="fk_mapping_paper_carrier",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prior_mapping_id"], ["paper_question_mappings.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_question_id"],
            ["questions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("paper_version_id", "legacy_question_id", name="uq_mapping_question"),
        sa.UniqueConstraint("paper_version_id", "printed_label", name="uq_mapping_label"),
        sa.UniqueConstraint("paper_version_id", "question_key", name="uq_mapping_question_key"),
        sa.UniqueConstraint("paper_version_id", "sort_order", name="uq_mapping_order"),
    )
    op.add_column("security_scopes", sa.Column("paper_version_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint("uq_security_scope_paper", "security_scopes", ["paper_version_id"])
    op.create_foreign_key(
        "fk_security_scope_paper", "security_scopes", "paper_versions", ["paper_version_id"], ["id"]
    )

    op.drop_constraint("ck_security_scope_shape", "security_scopes", type_="check")
    op.create_check_constraint(
        "ck_security_scope_shape",
        "security_scopes",
        "(kind='installation' AND installation_id=1 AND assignment_id IS NULL AND"
        " paper_version_id IS NULL) OR (kind='assignment' AND installation_id IS "
        "NULL AND assignment_id IS NOT NULL AND paper_version_id IS NULL) OR "
        "(kind='paper_version' AND installation_id IS NULL AND assignment_id IS "
        "NULL AND paper_version_id IS NOT NULL)",
    )
    op.execute(
        """
CREATE FUNCTION paper_json_digest(value jsonb) RETURNS text LANGUAGE sql IMMUTABLE AS $$
 SELECT encode(sha256(convert_to(value::text,'UTF8')),'hex')
$$
"""
    )
    op.execute(
        """
CREATE FUNCTION paper_document(aid uuid) RETURNS jsonb LANGUAGE sql STABLE AS $$
 SELECT jsonb_build_object('assignment',
   (SELECT jsonb_build_object('title',title,'subject',subject,'grade_level',grade_level,
                             'description',description) FROM assignments WHERE id=aid),
 'questions', COALESCE((SELECT jsonb_agg(jsonb_build_object(
   'question_number',q.question_number,'question_type',q.question_type,'stem',q.stem,
   'max_score',q.max_score::text,
   'answer_key',(SELECT jsonb_build_object('answer_content',answer_content,'source',source)
                 FROM answer_keys WHERE question_id=q.id),
   'rubric',COALESCE((SELECT jsonb_agg(jsonb_build_object('description',description,
                      'points',points::text,'sort_order',sort_order) ORDER BY sort_order)
                      FROM rubric_criteria WHERE question_id=q.id),'[]'::jsonb)
 ) ORDER BY q.question_number) FROM questions q WHERE q.assignment_id=aid),'[]'::jsonb))
$$
"""
    )
    op.execute(
        """
CREATE FUNCTION paper_digests(aid uuid) RETURNS jsonb LANGUAGE sql STABLE AS $$
 WITH d AS (SELECT paper_document(aid) AS doc)
 SELECT jsonb_build_object('content_sha256',paper_json_digest(doc),
  'question_digest',paper_json_digest(COALESCE((SELECT jsonb_agg(value-'rubric'-'answer_key'
    ORDER BY (value->>'question_number')::int) FROM
jsonb_array_elements(doc->'questions')),'[]')),
  'rubric_digest',paper_json_digest(COALESCE((SELECT jsonb_agg(jsonb_build_object(
    'question_number',value->'question_number','max_score',value->'max_score',
    'rubric',value->'rubric') ORDER BY (value->>'question_number')::int)
    FROM jsonb_array_elements(doc->'questions')),'[]'))) FROM d
$$
"""
    )
    op.execute(
        """
CREATE FUNCTION paper_mapping_digest(pid uuid) RETURNS text LANGUAGE sql STABLE AS $$
 SELECT paper_json_digest(COALESCE(jsonb_agg(jsonb_build_object(
  'question_key',question_key,'legacy_question_id',legacy_question_id,
  'printed_label',printed_label,'sort_order',sort_order,'source_question_id',source_question_id,
  'source_rubric_sha256',source_rubric_sha256,'prior_mapping_id',prior_mapping_id
 ) ORDER BY sort_order),'[]'::jsonb)) FROM paper_question_mappings WHERE
paper_version_id=pid
$$
"""
    )
    op.execute(
        """
CREATE FUNCTION guard_paper_version() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE classification text; d jsonb; previous paper_versions;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'paper_version_immutable' USING
ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' THEN
  SELECT security_classification INTO classification FROM assignments
    WHERE id=NEW.canonical_assignment_id FOR UPDATE;
  IF classification IS DISTINCT FROM 'exam_protected' OR NEW.status<>'draft'
     OR NEW.locked_at IS NOT NULL OR NEW.revision<>1 THEN
    RAISE EXCEPTION 'paper_carrier_invalid' USING ERRCODE='23514';
  END IF;
  IF NEW.prior_version_id IS NULL THEN
   IF NEW.version_no<>1 THEN
    RAISE EXCEPTION 'paper_lineage_invalid' USING ERRCODE='23514'; END IF;
  ELSE
   SELECT * INTO previous FROM paper_versions WHERE id=NEW.prior_version_id FOR SHARE;
   IF previous.paper_family_id IS DISTINCT FROM NEW.paper_family_id
    OR previous.version_no+1<>NEW.version_no OR previous.status='draft' THEN
    RAISE EXCEPTION 'paper_lineage_invalid' USING ERRCODE='23514'; END IF;
  END IF;
  RETURN NEW;
 END IF;
 IF OLD.status<>'draft' THEN
  IF OLD.status='locked' AND NEW.status='retired' AND NEW.revision=OLD.revision+1
    AND to_jsonb(NEW)-'status'-'revision'=to_jsonb(OLD)-'status'-'revision' THEN RETURN
NEW; END IF;
  RAISE EXCEPTION 'paper_version_immutable' USING ERRCODE='23514';
 END IF;
 IF NEW.revision<>OLD.revision+1 OR
   to_jsonb(NEW)-ARRAY['status','revision','content_sha256','question_digest','rubric_digest',
      'mapping_digest','locked_at'] <>
to_jsonb(OLD)-ARRAY['status','revision','content_sha256',
      'question_digest','rubric_digest','mapping_digest','locked_at'] THEN
  RAISE EXCEPTION 'paper_identity_immutable' USING ERRCODE='23514';
 END IF;
 IF NEW.status='locked' THEN
  IF NOT EXISTS(SELECT 1 FROM questions WHERE assignment_id=NEW.canonical_assignment_id)
   OR EXISTS(SELECT 1 FROM questions q WHERE
q.assignment_id=NEW.canonical_assignment_id AND (
      NOT EXISTS(SELECT 1 FROM answer_keys WHERE question_id=q.id)
      OR COALESCE((SELECT sum(points) FROM rubric_criteria WHERE
question_id=q.id),0)<>q.max_score
      OR NOT EXISTS(SELECT 1 FROM paper_question_mappings WHERE paper_version_id=NEW.id
                    AND legacy_question_id=q.id)))
   OR NOT EXISTS(SELECT 1 FROM assignments WHERE id=NEW.canonical_assignment_id AND
status='published')
  THEN RAISE EXCEPTION 'paper_incomplete' USING ERRCODE='23514'; END IF;
  d := paper_digests(NEW.canonical_assignment_id);
  IF d->>'content_sha256'<>NEW.content_sha256 OR d->>'question_digest'<>NEW.question_digest
     OR d->>'rubric_digest'<>NEW.rubric_digest OR
paper_mapping_digest(NEW.id)<>NEW.mapping_digest THEN
   RAISE EXCEPTION 'paper_digest_mismatch' USING ERRCODE='23514';
  END IF;
  NEW.locked_at:=clock_timestamp();
 ELSIF NEW.status NOT IN ('draft','retired') THEN
  RAISE EXCEPTION 'paper_state_invalid' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$
"""
    )
    op.execute(
        """
CREATE FUNCTION assert_paper_content_editable(aid uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE state text;
BEGIN
 SELECT status INTO state FROM paper_versions WHERE canonical_assignment_id=aid FOR SHARE;
 IF state IS NOT NULL AND state<>'draft' THEN
  RAISE EXCEPTION 'paper_content_locked' USING ERRCODE='23514';
 END IF;
END $$
"""
    )
    op.execute(
        """
CREATE FUNCTION guard_paper_content() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE old_aid uuid; new_aid uuid;
BEGIN
 IF TG_TABLE_NAME='assignments' THEN
   IF TG_OP<>'INSERT' THEN old_aid:=OLD.id; END IF;
   IF TG_OP<>'DELETE' THEN new_aid:=NEW.id; END IF;
 ELSIF TG_TABLE_NAME='questions' THEN
   IF TG_OP<>'INSERT' THEN old_aid:=OLD.assignment_id; END IF;
   IF TG_OP<>'DELETE' THEN new_aid:=NEW.assignment_id; END IF;
 ELSE
   IF TG_OP<>'INSERT' THEN SELECT assignment_id INTO old_aid FROM questions WHERE
id=OLD.question_id; END IF;
   IF TG_OP<>'DELETE' THEN SELECT assignment_id INTO new_aid FROM questions WHERE
id=NEW.question_id; END IF;
 END IF;
 -- Stable lock ordering when a raw writer moves a child between carriers.
 PERFORM assert_paper_content_editable(aid) FROM
   (SELECT DISTINCT x AS aid FROM unnest(ARRAY[old_aid,new_aid]) x WHERE x IS NOT NULL
ORDER BY x) ids;
 IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$
"""
    )
    op.execute(
        """
CREATE FUNCTION guard_paper_mapping() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent paper_versions; previous paper_question_mappings; pid uuid;
BEGIN
 FOR pid IN SELECT DISTINCT x FROM unnest(ARRAY[
   CASE WHEN TG_OP<>'INSERT' THEN OLD.paper_version_id END,
   CASE WHEN TG_OP<>'DELETE' THEN NEW.paper_version_id END]) x WHERE x IS NOT NULL
ORDER BY x
 LOOP
  SELECT * INTO parent FROM paper_versions WHERE id=pid FOR SHARE;
  IF parent.status<>'draft' THEN
   RAISE EXCEPTION 'paper_content_locked' USING ERRCODE='23514';
  END IF;
 END LOOP;
 IF TG_OP<>'DELETE' AND NEW.prior_mapping_id IS NOT NULL THEN
  SELECT * INTO previous FROM paper_question_mappings WHERE id=NEW.prior_mapping_id;
  SELECT * INTO parent FROM paper_versions WHERE id=NEW.paper_version_id;
  IF previous.paper_version_id IS DISTINCT FROM parent.prior_version_id
     OR previous.question_key IS DISTINCT FROM NEW.question_key THEN
   RAISE EXCEPTION 'paper_mapping_lineage_invalid' USING ERRCODE='23514';
  END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$
"""
    )
    for table in ("assignments", "questions", "answer_keys", "rubric_criteria"):
        op.execute(
            f"CREATE TRIGGER freeze_paper_content BEFORE INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION guard_paper_content()"
        )
    op.execute(
        "CREATE TRIGGER freeze_paper_version BEFORE INSERT OR UPDATE OR DELETE ON"
        " paper_versions FOR EACH ROW EXECUTE FUNCTION guard_paper_version()"
    )
    op.execute(
        "CREATE TRIGGER freeze_paper_mapping BEFORE INSERT OR UPDATE OR DELETE ON"
        " paper_question_mappings FOR EACH ROW EXECUTE FUNCTION "
        "guard_paper_mapping()"
    )
    for table in ("paper_families", "paper_operations"):
        op.execute(
            f"CREATE TRIGGER immutable_paper_record BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION guard_security_immutable()"
        )
    bind = op.get_bind()
    for code in ("paper.create", "paper.read", "paper.edit", "paper.lock"):
        kinds = ["installation"] if code == "paper.create" else ["installation", "paper_version"]
        bind.execute(
            sa.text("INSERT INTO permission_definitions VALUES (:code,true,:kinds)"),
            dict(code=code, kinds=kinds),
        )
    codes = [
        "security.grants.manage",
        "security.protected.create",
        "security.classification.inspect",
        "organization.read",
        "organization.manage",
        "student.import.preview",
        "student.import.confirm",
        "paper.create",
        "paper.read",
        "paper.edit",
        "paper.lock",
    ]
    for suffix, name, permissions in [
        ("1", "paper_operator", codes),
        ("2", "paper_editor", ["paper.read", "paper.edit"]),
        ("3", "paper_locker", ["paper.read", "paper.lock"]),
        ("4", "paper_reader", ["paper.read"]),
    ]:
        role_id = "11111111-b12a-4000-8000-00000000000" + suffix
        bind.execute(
            sa.text("INSERT INTO security_role_versions VALUES (CAST(:id AS uuid),:code,1,NULL)"),
            dict(id=role_id, code=name),
        )
        for code in permissions:
            bind.execute(
                sa.text("INSERT INTO security_role_permissions VALUES (CAST(:id AS uuid),:code)"),
                dict(id=role_id, code=code),
            )
        bind.execute(
            sa.text(
                "UPDATE security_role_versions SET sealed_at=clock_timestamp() WHERE "
                "id=CAST(:id AS uuid)"
            ),
            dict(id=role_id),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM paper_families) OR EXISTS(SELECT 1 FROM "
            "paper_versions) OR EXISTS(SELECT 1 FROM paper_operations) OR "
            "EXISTS(SELECT 1 FROM security_scopes WHERE kind='paper_version') OR "
            "EXISTS(SELECT 1 FROM security_grants WHERE role_version_id::text LIKE "
            "'11111111-b12a-%')"
        )
    ):
        raise RuntimeError("paper_downgrade_requires_archived_new_data")
    for table in ("assignments", "questions", "answer_keys", "rubric_criteria"):
        op.execute(f"DROP TRIGGER freeze_paper_content ON {table}")
    op.execute("DROP TRIGGER freeze_paper_version ON paper_versions")
    op.execute("DROP TRIGGER freeze_paper_mapping ON paper_question_mappings")
    for function in (
        "guard_paper_mapping()",
        "guard_paper_content()",
        "assert_paper_content_editable(uuid)",
        "guard_paper_version()",
        "paper_mapping_digest(uuid)",
        "paper_digests(uuid)",
        "paper_document(uuid)",
        "paper_json_digest(jsonb)",
    ):
        op.execute("DROP FUNCTION " + function)
    op.execute("ALTER TABLE security_role_permissions DISABLE TRIGGER USER")
    op.execute("ALTER TABLE security_role_versions DISABLE TRIGGER USER")
    op.execute(
        "DELETE FROM security_role_permissions WHERE role_version_id::text LIKE '11111111-b12a-%'"
    )
    op.execute("DELETE FROM security_role_versions WHERE id::text LIKE '11111111-b12a-%'")
    op.execute(
        "DELETE FROM permission_definitions WHERE code IN "
        "('paper.create','paper.read','paper.edit','paper.lock')"
    )
    op.execute("ALTER TABLE security_role_permissions ENABLE TRIGGER USER")
    op.execute("ALTER TABLE security_role_versions ENABLE TRIGGER USER")
    op.drop_constraint("ck_security_scope_shape", "security_scopes", type_="check")
    op.drop_constraint("fk_security_scope_paper", "security_scopes", type_="foreignkey")
    op.drop_constraint("uq_security_scope_paper", "security_scopes", type_="unique")
    op.drop_column("security_scopes", "paper_version_id")
    op.create_check_constraint(
        "ck_security_scope_shape",
        "security_scopes",
        "(kind='installation' AND installation_id=1 AND assignment_id IS NULL) OR"
        " (kind='assignment' AND installation_id IS NULL AND assignment_id IS NOT"
        " NULL)",
    )
    op.drop_table("paper_question_mappings")
    op.drop_table("paper_versions")
    op.drop_table("paper_families")
    op.drop_table("paper_operations")
    op.drop_constraint("uq_question_parent_pair", "questions", type_="unique")
