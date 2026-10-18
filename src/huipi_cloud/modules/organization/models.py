"""Single-school organization: academic calendar, homeroom classes, students, enrollment.

B-1.1b scope: trusted academic years/terms, admission cohorts (grade labels are derived,
never a permanent student attribute), homeroom classes, minimal student identity,
history-preserving class enrollment and two-phase roster import. Teaching classes,
exam sessions, papers, participation, scanning and marking are explicitly out of scope.
"""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from huipi_cloud.infrastructure.database.base import Base, utc_now


class AcademicYear(Base):
    __tablename__ = "academic_years"
    __table_args__ = (
        UniqueConstraint("code", name="uq_academic_years_code"),
        CheckConstraint("length(trim(code)) > 0", name="ck_academic_years_code_not_blank"),
        CheckConstraint(
            "length(trim(display_name)) > 0", name="ck_academic_years_name_not_blank"
        ),
        CheckConstraint("end_date > start_date", name="ck_academic_years_date_order"),
        CheckConstraint(
            "status IN ('planned','active','closed')", name="ck_academic_years_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="planned", server_default="planned"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    terms: Mapped[list["AcademicTerm"]] = relationship(
        back_populates="academic_year",
        order_by="AcademicTerm.term_no",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AcademicTerm(Base):
    __tablename__ = "academic_terms"
    __table_args__ = (
        UniqueConstraint("academic_year_id", "term_no", name="uq_academic_terms_year_no"),
        UniqueConstraint("academic_year_id", "code", name="uq_academic_terms_year_code"),
        CheckConstraint("term_no > 0", name="ck_academic_terms_no_positive"),
        CheckConstraint("length(trim(code)) > 0", name="ck_academic_terms_code_not_blank"),
        CheckConstraint("end_date > start_date", name="ck_academic_terms_date_order"),
        ExcludeConstraint(
            ("academic_year_id", "="),
            (text("daterange(start_date, end_date, '[]')"), "&&"),
            name="ex_academic_terms_year_range",
            using="gist",
        ),
        Index("ix_academic_terms_year_id", "academic_year_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    academic_year_id: Mapped[UUID] = mapped_column(
        ForeignKey("academic_years.id", ondelete="RESTRICT"), nullable=False
    )
    term_no: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )

    academic_year: Mapped[AcademicYear] = relationship(back_populates="terms")


class GradeCohort(Base):
    """Admission cohort (e.g. 2025 intake); grade labels are derived per academic year."""

    __tablename__ = "grade_cohorts"
    __table_args__ = (
        UniqueConstraint("code", name="uq_grade_cohorts_code"),
        CheckConstraint("length(trim(code)) > 0", name="ck_grade_cohorts_code_not_blank"),
        CheckConstraint(
            "length(trim(display_name)) > 0", name="ck_grade_cohorts_name_not_blank"
        ),
        CheckConstraint(
            "intake_year BETWEEN 2000 AND 2100", name="ck_grade_cohorts_intake_year"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    intake_year: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class Subject(Base):
    __tablename__ = "subjects"
    __table_args__ = (
        UniqueConstraint("code", name="uq_subjects_code"),
        CheckConstraint("length(trim(code)) > 0", name="ck_subjects_code_not_blank"),
        CheckConstraint("length(trim(display_name)) > 0", name="ck_subjects_name_not_blank"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class AdministrativeClass(Base):
    """Homeroom class; not a teaching class, exam room or seating plan."""

    __tablename__ = "administrative_classes"
    __table_args__ = (
        UniqueConstraint("code", name="uq_administrative_classes_code"),
        CheckConstraint(
            "length(trim(code)) > 0", name="ck_administrative_classes_code_not_blank"
        ),
        CheckConstraint(
            "length(trim(display_name)) > 0", name="ck_administrative_classes_name_not_blank"
        ),
        CheckConstraint(
            "status IN ('active','inactive')", name="ck_administrative_classes_status"
        ),
        Index("ix_administrative_classes_cohort", "grade_cohort_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    grade_cohort_id: Mapped[UUID] = mapped_column(
        ForeignKey("grade_cohorts.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class Student(Base):
    """Stable internal identity; names are neither keys nor automatic merge criteria."""

    __tablename__ = "students"
    __table_args__ = (
        UniqueConstraint("school_student_code", name="uq_students_school_code"),
        CheckConstraint(
            "length(trim(school_student_code)) > 0", name="ck_students_code_not_blank"
        ),
        CheckConstraint("length(trim(name)) > 0", name="ck_students_name_not_blank"),
        Index("ix_students_name", "name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    school_student_code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class ClassEnrollment(Base):
    """Half-open ``[valid_from, valid_to)`` homeroom membership with full history.

    A student may have at most one homeroom at any instant; PostgreSQL enforces this
    with a GiST exclusion constraint rather than a Python read-then-insert check.
    """

    __tablename__ = "class_enrollments"
    __table_args__ = (
        ExcludeConstraint(
            ("student_id", "="),
            (text("daterange(valid_from, valid_to, '[)')"), "&&"),
            name="ex_class_enrollments_student_time",
            using="gist",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_class_enrollments_range"
        ),
        CheckConstraint(
            "source IN ('manual','student_import')", name="ck_class_enrollments_source"
        ),
        Index("ix_class_enrollments_class_time", "administrative_class_id", "valid_from"),
        Index("ix_class_enrollments_student", "student_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    student_id: Mapped[UUID] = mapped_column(
        ForeignKey("students.id", ondelete="RESTRICT"), nullable=False
    )
    administrative_class_id: Mapped[UUID] = mapped_column(
        ForeignKey("administrative_classes.id", ondelete="RESTRICT"), nullable=False
    )
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="manual", server_default="manual"
    )
    source_import_batch_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("student_import_batches.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class StudentImportBatch(Base):
    """Frozen preview of one roster CSV; confirmation is a separate audited operation."""

    __tablename__ = "student_import_batches"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('student_only','enrollment_only','student_with_enrollment')",
            name="ck_student_import_batches_mode",
        ),
        CheckConstraint(
            "status IN ('previewed','confirmed')", name="ck_student_import_batches_status"
        ),
        CheckConstraint("length(input_sha256) = 64", name="ck_student_import_batches_sha"),
        CheckConstraint("file_size_bytes > 0", name="ck_student_import_batches_size"),
        CheckConstraint("row_count > 0", name="ck_student_import_batches_rows"),
        CheckConstraint(
            "jsonb_typeof(preview_counts) = 'object'",
            name="ck_student_import_batches_counts",
        ),
        Index("ix_student_import_batches_requester", "requested_by_user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="previewed", server_default="previewed"
    )
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    academic_year_id: Mapped[UUID] = mapped_column(
        ForeignKey("academic_years.id", ondelete="RESTRICT"), nullable=False
    )
    requested_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    preview_counts: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confirm_result: Mapped[dict | None] = mapped_column(JSONB)
    confirmed_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )

    rows: Mapped[list["StudentImportRow"]] = relationship(
        back_populates="batch",
        order_by="StudentImportRow.row_number",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class StudentImportRow(Base):
    """Immutable parsed preview row; names/codes never enter ordinary logs."""

    __tablename__ = "student_import_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="uq_student_import_rows_batch_row"),
        CheckConstraint("row_number > 1", name="ck_student_import_rows_number"),
        CheckConstraint(
            "row_state IN ('valid','warning','error')", name="ck_student_import_rows_state"
        ),
        CheckConstraint("jsonb_typeof(errors) = 'array'", name="ck_student_import_rows_errors"),
        CheckConstraint(
            "jsonb_typeof(warnings) = 'array'", name="ck_student_import_rows_warnings"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("student_import_batches.id", ondelete="RESTRICT"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    student_code: Mapped[str | None] = mapped_column(String(64))
    normalized_student_code: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(100))
    class_code: Mapped[str | None] = mapped_column(String(32))
    valid_from: Mapped[date | None] = mapped_column(Date)
    row_state: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32))
    errors: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    warnings: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    batch: Mapped[StudentImportBatch] = relationship(back_populates="rows")


class OrganizationOperation(Base):
    """Append-only organization audit and idempotency facts; never student payloads."""

    __tablename__ = "organization_operations"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id", "request_id", name="uq_organization_operations_request"
        ),
        CheckConstraint(
            "length(payload_sha256) = 64", name="ck_organization_operations_digest"
        ),
        CheckConstraint(
            "jsonb_typeof(result) = 'object'", name="ck_organization_operations_result"
        ),
        CheckConstraint(
            "action IN ('academic_year.created','academic_term.created','grade_cohort.created',"
            "'subject.created','administrative_class.created','class_enrollment.created',"
            "'student.transferred','student_import.previewed','student_import.confirmed')",
            name="ck_organization_operations_action",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
