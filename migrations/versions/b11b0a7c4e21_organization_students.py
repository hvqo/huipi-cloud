"""B-1.1b: single-school organization, trusted students and two-phase roster import.

Revision ID: b11b0a7c4e21
Revises: e21a9b4c110a
Create Date: 2026-10-18
"""

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b11b0a7c4e21"
down_revision = "e21a9b4c110a"
branch_labels = None
depends_on = None

INSTALLATION = ["installation"]

ORGANIZATION_OPERATOR_ROLE = UUID("11111111-b11b-4000-8000-000000000001")
ORGANIZATION_MANAGER_ROLE = UUID("11111111-b11b-4000-8000-000000000002")
STUDENT_IMPORTER_ROLE = UUID("11111111-b11b-4000-8000-000000000003")
GRANT_INDEX = "uq_security_grants_active"


def uid(name, target=None, *, primary_key=False, nullable=False):
    args = [sa.ForeignKey(target, ondelete="RESTRICT")] if target else []
    return sa.Column(name, sa.Uuid(), *args, primary_key=primary_key, nullable=nullable)


def upgrade():
    # uuid equality inside GiST exclusion constraints (term ranges / enrollment ranges).
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "academic_years",
        uid("id", primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="planned"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("code", name="uq_academic_years_code"),
        sa.CheckConstraint("length(trim(code)) > 0", name="ck_academic_years_code_not_blank"),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0", name="ck_academic_years_name_not_blank"
        ),
        sa.CheckConstraint("end_date > start_date", name="ck_academic_years_date_order"),
        sa.CheckConstraint(
            "status IN ('planned','active','closed')", name="ck_academic_years_status"
        ),
    )
    op.create_table(
        "academic_terms",
        uid("id", primary_key=True),
        uid("academic_year_id", "academic_years.id"),
        sa.Column("term_no", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("academic_year_id", "term_no", name="uq_academic_terms_year_no"),
        sa.UniqueConstraint("academic_year_id", "code", name="uq_academic_terms_year_code"),
        sa.CheckConstraint("term_no > 0", name="ck_academic_terms_no_positive"),
        sa.CheckConstraint("length(trim(code)) > 0", name="ck_academic_terms_code_not_blank"),
        sa.CheckConstraint("end_date > start_date", name="ck_academic_terms_date_order"),
        postgresql.ExcludeConstraint(
            ("academic_year_id", "="),
            (sa.text("daterange(start_date, end_date, '[]')"), "&&"),
            name="ex_academic_terms_year_range",
            using="gist",
        ),
    )
    op.create_index("ix_academic_terms_year_id", "academic_terms", ["academic_year_id"])
    op.create_table(
        "grade_cohorts",
        uid("id", primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("intake_year", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("code", name="uq_grade_cohorts_code"),
        sa.CheckConstraint("length(trim(code)) > 0", name="ck_grade_cohorts_code_not_blank"),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0", name="ck_grade_cohorts_name_not_blank"
        ),
        sa.CheckConstraint(
            "intake_year BETWEEN 2000 AND 2100", name="ck_grade_cohorts_intake_year"
        ),
    )
    op.create_table(
        "subjects",
        uid("id", primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("code", name="uq_subjects_code"),
        sa.CheckConstraint("length(trim(code)) > 0", name="ck_subjects_code_not_blank"),
        sa.CheckConstraint("length(trim(display_name)) > 0", name="ck_subjects_name_not_blank"),
    )
    op.create_table(
        "administrative_classes",
        uid("id", primary_key=True),
        uid("grade_cohort_id", "grade_cohorts.id"),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("code", name="uq_administrative_classes_code"),
        sa.CheckConstraint(
            "length(trim(code)) > 0", name="ck_administrative_classes_code_not_blank"
        ),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0", name="ck_administrative_classes_name_not_blank"
        ),
        sa.CheckConstraint(
            "status IN ('active','inactive')", name="ck_administrative_classes_status"
        ),
    )
    op.create_index(
        "ix_administrative_classes_cohort",
        "administrative_classes",
        ["grade_cohort_id", "status"],
    )
    op.create_table(
        "students",
        uid("id", primary_key=True),
        sa.Column("school_student_code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("school_student_code", name="uq_students_school_code"),
        sa.CheckConstraint(
            "length(trim(school_student_code)) > 0", name="ck_students_code_not_blank"
        ),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_students_name_not_blank"),
    )
    op.create_index("ix_students_name", "students", ["name"])
    op.create_table(
        "student_import_batches",
        uid("id", primary_key=True),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="previewed"),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        uid("academic_year_id", "academic_years.id"),
        uid("requested_by_user_id", "users.id"),
        sa.Column("preview_counts", postgresql.JSONB(), nullable=False),
        sa.Column("confirm_result", postgresql.JSONB()),
        uid("confirmed_by_user_id", "users.id", nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "mode IN ('student_only','enrollment_only','student_with_enrollment')",
            name="ck_student_import_batches_mode",
        ),
        sa.CheckConstraint(
            "status IN ('previewed','confirmed')", name="ck_student_import_batches_status"
        ),
        sa.CheckConstraint("length(input_sha256) = 64", name="ck_student_import_batches_sha"),
        sa.CheckConstraint("file_size_bytes > 0", name="ck_student_import_batches_size"),
        sa.CheckConstraint("row_count > 0", name="ck_student_import_batches_rows"),
        sa.CheckConstraint(
            "jsonb_typeof(preview_counts) = 'object'",
            name="ck_student_import_batches_counts",
        ),
    )
    op.create_index(
        "ix_student_import_batches_requester",
        "student_import_batches",
        ["requested_by_user_id", "created_at"],
    )
    op.create_table(
        "student_import_rows",
        uid("id", primary_key=True),
        uid("batch_id", "student_import_batches.id"),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("student_code", sa.String(64)),
        sa.Column("normalized_student_code", sa.String(64)),
        sa.Column("name", sa.String(100)),
        sa.Column("class_code", sa.String(32)),
        sa.Column("valid_from", sa.Date()),
        sa.Column("row_state", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(32)),
        sa.Column(
            "errors", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "warnings", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.UniqueConstraint("batch_id", "row_number", name="uq_student_import_rows_batch_row"),
        sa.CheckConstraint("row_number > 1", name="ck_student_import_rows_number"),
        sa.CheckConstraint(
            "row_state IN ('valid','warning','error')", name="ck_student_import_rows_state"
        ),
        sa.CheckConstraint("jsonb_typeof(errors) = 'array'", name="ck_student_import_rows_errors"),
        sa.CheckConstraint(
            "jsonb_typeof(warnings) = 'array'", name="ck_student_import_rows_warnings"
        ),
    )
    op.create_table(
        "class_enrollments",
        uid("id", primary_key=True),
        uid("student_id", "students.id"),
        uid("administrative_class_id", "administrative_classes.id"),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date()),
        sa.Column("source", sa.String(24), nullable=False, server_default="manual"),
        uid("source_import_batch_id", "student_import_batches.id", nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        postgresql.ExcludeConstraint(
            ("student_id", "="),
            (sa.text("daterange(valid_from, valid_to, '[)')"), "&&"),
            name="ex_class_enrollments_student_time",
            using="gist",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_class_enrollments_range"
        ),
        sa.CheckConstraint(
            "source IN ('manual','student_import')", name="ck_class_enrollments_source"
        ),
    )
    op.create_index(
        "ix_class_enrollments_class_time",
        "class_enrollments",
        ["administrative_class_id", "valid_from"],
    )
    op.create_index("ix_class_enrollments_student", "class_enrollments", ["student_id"])
    op.create_table(
        "organization_operations",
        uid("id", primary_key=True),
        uid("actor_user_id"),
        uid("request_id"),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "actor_user_id", "request_id", name="uq_organization_operations_request"
        ),
        sa.CheckConstraint(
            "length(payload_sha256) = 64", name="ck_organization_operations_digest"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result) = 'object'", name="ck_organization_operations_result"
        ),
        sa.CheckConstraint(
            "action IN ('academic_year.created','academic_term.created','grade_cohort.created',"
            "'subject.created','administrative_class.created','class_enrollment.created',"
            "'student.transferred','student_import.previewed','student_import.confirmed')",
            name="ck_organization_operations_action",
        ),
    )

    # B-1.1a audit follow-up: at most one active Grant per user/role/scope. Operators must
    # resolve existing duplicates before upgrading; the API maps a duplicate to 409.
    duplicates = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM (SELECT 1 FROM security_grants WHERE revoked_at IS NULL "
            "GROUP BY user_id, role_version_id, scope_id HAVING count(*) > 1) AS duplicated"
        )
    )
    if duplicates:
        raise RuntimeError("security_grant_duplicates_require_cleanup")
    op.create_index(
        GRANT_INDEX,
        "security_grants",
        ["user_id", "role_version_id", "scope_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    # Terms must fit inside their academic year; a cross-row rule needs a trigger because
    # PostgreSQL CHECK constraints cannot reference another table.
    op.execute("""
      CREATE FUNCTION validate_academic_term_range() RETURNS trigger AS $$
      DECLARE year_start date; year_end date;
      BEGIN
        SELECT start_date, end_date INTO year_start, year_end
          FROM academic_years WHERE id = NEW.academic_year_id;
        IF year_start IS NULL THEN
          RAISE EXCEPTION 'academic_term_year_missing' USING ERRCODE='23503';
        END IF;
        IF NEW.start_date < year_start OR NEW.end_date > year_end THEN
          RAISE EXCEPTION 'academic_term_outside_year' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
      CREATE TRIGGER valid_academic_term_range BEFORE INSERT OR UPDATE ON academic_terms
      FOR EACH ROW EXECUTE FUNCTION validate_academic_term_range()
    """)
    op.execute("""
      CREATE FUNCTION guard_organization_immutable() RETURNS trigger AS $$
      BEGIN RAISE EXCEPTION 'organization_record_immutable' USING ERRCODE='23514'; END;
      $$ LANGUAGE plpgsql
    """)
    op.execute("""
      CREATE TRIGGER immutable_organization_operations
      BEFORE UPDATE OR DELETE ON organization_operations
      FOR EACH ROW EXECUTE FUNCTION guard_organization_immutable()
    """)
    op.execute("""
      CREATE TRIGGER immutable_student_import_rows
      BEFORE UPDATE OR DELETE ON student_import_rows
      FOR EACH ROW EXECUTE FUNCTION guard_organization_immutable()
    """)
    op.execute("""
      CREATE FUNCTION protect_student_import_batch() RETURNS trigger AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN
          RAISE EXCEPTION 'student_import_batch_delete_forbidden' USING ERRCODE='23514';
        END IF;
        IF OLD.status = 'confirmed' THEN
          RAISE EXCEPTION 'student_import_batch_immutable' USING ERRCODE='23514';
        END IF;
        IF NEW.status <> 'confirmed'
          OR NEW.mode IS DISTINCT FROM OLD.mode
          OR NEW.input_sha256 IS DISTINCT FROM OLD.input_sha256
          OR NEW.file_size_bytes IS DISTINCT FROM OLD.file_size_bytes
          OR NEW.row_count IS DISTINCT FROM OLD.row_count
          OR NEW.academic_year_id IS DISTINCT FROM OLD.academic_year_id
          OR NEW.requested_by_user_id IS DISTINCT FROM OLD.requested_by_user_id
          OR NEW.preview_counts IS DISTINCT FROM OLD.preview_counts
          OR NEW.created_at IS DISTINCT FROM OLD.created_at
        THEN
          RAISE EXCEPTION 'student_import_batch_update_forbidden' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
      CREATE TRIGGER protect_student_import_batch BEFORE UPDATE OR DELETE
      ON student_import_batches FOR EACH ROW EXECUTE FUNCTION protect_student_import_batch()
    """)
    # Enrollment history: identity columns immutable, valid_to set at most once, no delete.
    op.execute("""
      CREATE FUNCTION protect_class_enrollment_history() RETURNS trigger AS $$
      BEGIN
        IF TG_OP = 'DELETE' THEN
          RAISE EXCEPTION 'class_enrollment_delete_forbidden' USING ERRCODE='23514';
        END IF;
        IF NEW.student_id IS DISTINCT FROM OLD.student_id
          OR NEW.administrative_class_id IS DISTINCT FROM OLD.administrative_class_id
          OR NEW.valid_from IS DISTINCT FROM OLD.valid_from
          OR NEW.source IS DISTINCT FROM OLD.source
          OR NEW.source_import_batch_id IS DISTINCT FROM OLD.source_import_batch_id
          OR NEW.created_at IS DISTINCT FROM OLD.created_at
          OR (OLD.valid_to IS NOT NULL AND NEW.valid_to IS DISTINCT FROM OLD.valid_to)
        THEN
          RAISE EXCEPTION 'class_enrollment_history_immutable' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql
    """)
    op.execute("""
      CREATE TRIGGER protect_class_enrollment_history BEFORE UPDATE OR DELETE
      ON class_enrollments FOR EACH ROW EXECUTE FUNCTION protect_class_enrollment_history()
    """)

    bind = op.get_bind()
    permissions = (
        "organization.read",
        "organization.manage",
        "student.import.preview",
        "student.import.confirm",
    )
    permission_table = sa.table(
        "permission_definitions",
        sa.column("code", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("scope_kinds", postgresql.ARRAY(sa.String())),
    )
    bind.execute(
        permission_table.insert(),
        [dict(code=code, enabled=True, scope_kinds=INSTALLATION) for code in permissions],
    )
    roles = [
        (
            ORGANIZATION_OPERATOR_ROLE,
            "organization_operator",
            [
                "security.grants.manage",
                "security.protected.create",
                "security.classification.inspect",
                *permissions,
            ],
        ),
        (
            ORGANIZATION_MANAGER_ROLE,
            "organization_manager",
            ["organization.read", "organization.manage"],
        ),
        (
            STUDENT_IMPORTER_ROLE,
            "student_importer",
            ["organization.read", "student.import.preview", "student.import.confirm"],
        ),
    ]
    for role_id, code, codes in roles:
        # The B-1.1a trigger forbids adding permissions to a sealed role version, so the
        # deployment migration inserts unsealed versions and seals them afterwards.
        bind.execute(
            sa.text("INSERT INTO security_role_versions VALUES (:id,:code,1,NULL)"),
            dict(id=role_id, code=code),
        )
        for permission in codes:
            bind.execute(
                sa.text("INSERT INTO security_role_permissions VALUES (:id,:permission)"),
                dict(id=role_id, permission=permission),
            )
        bind.execute(
            sa.text("UPDATE security_role_versions SET sealed_at=:sealed WHERE id=:id"),
            dict(id=role_id, sealed=datetime.now(UTC)),
        )


def downgrade():
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM students) OR EXISTS(SELECT 1 FROM class_enrollments)"
            " OR EXISTS(SELECT 1 FROM administrative_classes) OR EXISTS(SELECT 1 FROM subjects)"
            " OR EXISTS(SELECT 1 FROM grade_cohorts) OR EXISTS(SELECT 1 FROM academic_terms)"
            " OR EXISTS(SELECT 1 FROM academic_years)"
            " OR EXISTS(SELECT 1 FROM student_import_batches)"
            " OR EXISTS(SELECT 1 FROM organization_operations)"
            " OR EXISTS(SELECT 1 FROM security_grants WHERE role_version_id IN ("
            "'11111111-b11b-4000-8000-000000000001'::uuid,"
            "'11111111-b11b-4000-8000-000000000002'::uuid,"
            "'11111111-b11b-4000-8000-000000000003'::uuid))"
        )
    ):
        raise RuntimeError("organization_downgrade_requires_archived_new_data")
    op.drop_index(GRANT_INDEX, table_name="security_grants")
    op.execute("DROP TRIGGER protect_class_enrollment_history ON class_enrollments")
    op.execute("DROP FUNCTION protect_class_enrollment_history()")
    op.execute("DROP TRIGGER protect_student_import_batch ON student_import_batches")
    op.execute("DROP FUNCTION protect_student_import_batch()")
    op.execute("DROP TRIGGER immutable_student_import_rows ON student_import_rows")
    op.execute("DROP TRIGGER immutable_organization_operations ON organization_operations")
    op.execute("DROP FUNCTION guard_organization_immutable()")
    op.execute("DROP TRIGGER valid_academic_term_range ON academic_terms")
    op.execute("DROP FUNCTION validate_academic_term_range()")
    # B-1.1a seals role versions and forbids deleting them through user triggers. This
    # deployment migration owns those rows, so it disables the user triggers only during
    # the controlled cleanup and re-enables them in the same transaction.
    op.execute("ALTER TABLE security_role_permissions DISABLE TRIGGER USER")
    op.execute("ALTER TABLE security_role_versions DISABLE TRIGGER USER")
    try:
        for role_id in (
            ORGANIZATION_OPERATOR_ROLE,
            ORGANIZATION_MANAGER_ROLE,
            STUDENT_IMPORTER_ROLE,
        ):
            op.execute(
                sa.text(
                    "DELETE FROM security_role_permissions WHERE role_version_id = :id"
                ).bindparams(id=role_id)
            )
            op.execute(
                sa.text("DELETE FROM security_role_versions WHERE id = :id").bindparams(
                    id=role_id
                )
            )
    finally:
        op.execute("ALTER TABLE security_role_permissions ENABLE TRIGGER USER")
        op.execute("ALTER TABLE security_role_versions ENABLE TRIGGER USER")
    op.execute(
        sa.text(
            "DELETE FROM permission_definitions WHERE code IN "
            "('organization.read','organization.manage',"
            "'student.import.preview','student.import.confirm')"
        )
    )
    op.drop_table("organization_operations")
    op.drop_index("ix_class_enrollments_student", table_name="class_enrollments")
    op.drop_index("ix_class_enrollments_class_time", table_name="class_enrollments")
    op.drop_table("class_enrollments")
    op.drop_table("student_import_rows")
    op.drop_index("ix_student_import_batches_requester", table_name="student_import_batches")
    op.drop_table("student_import_batches")
    op.drop_index("ix_students_name", table_name="students")
    op.drop_table("students")
    op.drop_index("ix_administrative_classes_cohort", table_name="administrative_classes")
    op.drop_table("administrative_classes")
    op.drop_table("subjects")
    op.drop_table("grade_cohorts")
    op.drop_index("ix_academic_terms_year_id", table_name="academic_terms")
    op.drop_table("academic_terms")
    op.drop_table("academic_years")
