"""Bounded projections for the school UI; identities/import decisions stay in original services."""

from datetime import date

from sqlalchemy import and_, func, or_, select

from huipi_cloud.modules.identity.protocol import AccessError

from . import service
from .models import (
    AcademicYear,
    AdministrativeClass,
    ClassEnrollment,
    GradeCohort,
    Student,
    StudentImportBatch,
    StudentImportRow,
)
from .operations import authorize


async def school_access(db, principal):
    """Identity metadata only, no dictionaries/student data; never based on is_admin."""
    async with db.begin():
        values = {}
        for name, action in [
            ("can_read", "organization.read"),
            ("can_manage", "organization.manage"),
            ("can_preview", "student.import.preview"),
            ("can_confirm", "student.import.confirm"),
        ]:
            try:
                await authorize(db, principal, action)
                values[name] = True
            except AccessError as error:
                if error.status_code not in {403, 404}:
                    raise
                values[name] = False
        return values


async def classes(db, principal, *, academic_year_id, cohort_id, as_of, limit, offset):
    async with db.begin():
        await authorize(db, principal, "organization.read")
        year = await db.get(AcademicYear, academic_year_id) if academic_year_id else None
        if academic_year_id and year is None:
            raise AccessError("academic_year_not_found", 404)
        effective = as_of or date.today()
        if year and not year.start_date <= effective <= year.end_date:
            raise AccessError("school_date_outside_academic_year", 422)
        counts = (
            select(
                ClassEnrollment.administrative_class_id.label("class_id"),
                func.count().label("student_count"),
            )
            .join(Student, Student.id == ClassEnrollment.student_id)
            .where(
                Student.is_active.is_(True),
                ClassEnrollment.valid_from <= effective,
                or_(ClassEnrollment.valid_to.is_(None), ClassEnrollment.valid_to > effective),
            )
            .group_by(ClassEnrollment.administrative_class_id)
            .subquery()
        )
        query = (
            select(
                AdministrativeClass,
                GradeCohort.intake_year,
                func.coalesce(counts.c.student_count, 0),
            )
            .join(GradeCohort, GradeCohort.id == AdministrativeClass.grade_cohort_id)
            .outerjoin(counts, counts.c.class_id == AdministrativeClass.id)
        )
        if cohort_id:
            query = query.where(AdministrativeClass.grade_cohort_id == cohort_id)
        if year:
            query = query.where(
                and_(
                    GradeCohort.intake_year <= year.start_date.year,
                    GradeCohort.intake_year >= year.start_date.year - 2,
                )
            )
        rows = (
            await db.execute(
                query.order_by(AdministrativeClass.code, AdministrativeClass.id)
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()

        def item(row):
            cls, intake, count = row
            label = service.grade_label(intake, year.start_date.year) if year else None
            return dict(
                **service._class_summary(cls),
                student_count=count,
                as_of=effective,
                grade_label=label[1] if label else None,
            )

        return service._envelope(rows, limit, offset, item)


async def import_rows(db, principal, batch_id, *, limit, offset):
    async with db.begin():
        await authorize(db, principal, "organization.read")
        if await db.get(StudentImportBatch, batch_id) is None:
            raise AccessError("resource_not_found_or_forbidden", 404)
        rows = (
            await db.scalars(
                select(StudentImportRow)
                .where(StudentImportRow.batch_id == batch_id)
                .order_by(StudentImportRow.row_number)
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()

        def item(row):
            return dict(
                row_number=row.row_number,
                student_code=row.student_code,
                name=row.name,
                class_code=row.class_code,
                valid_from=row.valid_from,
                row_state=row.row_state,
                outcome=row.outcome,
                errors=[dict(issue, row_number=row.row_number) for issue in row.errors],
                warnings=[dict(issue, row_number=row.row_number) for issue in row.warnings],
            )

        return dict(batch_id=batch_id, **service._envelope(rows, limit, offset, item))
