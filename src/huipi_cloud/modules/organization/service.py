"""Organization queries and audited writes.

B-1.1b only enables installation-scoped organization actions: there is no school /
grade / class scoped grant yet. Reads here serve trusted organization administrators;
they are not the future "teacher reads own class roster" contract, which needs a real
teaching relation and a class scope first.
"""

import unicodedata
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.organization.models import (
    AcademicTerm,
    AcademicYear,
    AdministrativeClass,
    ClassEnrollment,
    GradeCohort,
    Student,
    Subject,
)
from huipi_cloud.modules.organization.operations import authorize, operation
from huipi_cloud.modules.organization.schemas import (
    AcademicTermCreate,
    AcademicYearCreate,
    AdministrativeClassCreate,
    GradeCohortCreate,
    StudentTransferRequest,
    SubjectCreate,
)

GRADE_LABELS = {1: "高一", 2: "高二", 3: "高三"}
CODE_CONFLICT = "organization_code_conflict"
STATE_CONFLICT = "organization_state_conflict"


def grade_label(intake_year: int, academic_year_start_year: int) -> tuple[int, str] | None:
    """Derive a cohort's grade for an academic year; never stored on the student.

    Grade number = academic year start year - intake year + 1. Only grades 1..3 are
    inside the three-year high school; anything else returns ``None`` (graduated or
    not yet admitted).
    """
    grade_no = academic_year_start_year - intake_year + 1
    label = GRADE_LABELS.get(grade_no)
    return (grade_no, label) if label is not None else None


def current_enrollment():
    """Enrollment in force now; the range is half-open ``[valid_from, valid_to)``.

    A future-dated enrollment is history, not the current class, so both bounds matter.
    """
    return and_(
        ClassEnrollment.valid_from <= func.current_date(),
        or_(
            ClassEnrollment.valid_to.is_(None),
            ClassEnrollment.valid_to > func.current_date(),
        ),
    )


def _normalize_query(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip()
    return "".join(char for char in text if not unicodedata.category(char).startswith("C"))


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped


def _envelope(rows, limit: int, offset: int, serializer) -> dict:
    return {
        "records": [serializer(row) for row in rows[:limit]],
        "limit": limit,
        "offset": offset,
        "has_next": len(rows) > limit,
        "next_offset": offset + limit if len(rows) > limit else None,
    }


def _constraint_name(error: IntegrityError) -> str:
    original = getattr(error, "orig", None)
    name = getattr(original, "constraint_name", None)
    return name or ""


def _conflict(error: IntegrityError, mapping: dict[str, str]) -> AccessError:
    name = _constraint_name(error)
    if name in mapping:
        return AccessError(mapping[name], 409)
    message = str(getattr(error, "orig", error))
    for token, code in mapping.items():
        if token in message:
            return AccessError(code, 409)
    return AccessError(STATE_CONFLICT, 409)


def _year_summary(row: AcademicYear) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "display_name": row.display_name,
        "start_date": row.start_date,
        "end_date": row.end_date,
        "status": row.status,
    }


def _term_summary(row: AcademicTerm) -> dict:
    return {
        "id": row.id,
        "academic_year_id": row.academic_year_id,
        "term_no": row.term_no,
        "code": row.code,
        "start_date": row.start_date,
        "end_date": row.end_date,
    }


def _cohort_summary(row: GradeCohort) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "display_name": row.display_name,
        "intake_year": row.intake_year,
    }


def _subject_summary(row: Subject) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "display_name": row.display_name,
        "is_active": row.is_active,
    }


def _class_summary(row: AdministrativeClass) -> dict:
    return {
        "id": row.id,
        "grade_cohort_id": row.grade_cohort_id,
        "code": row.code,
        "display_name": row.display_name,
        "status": row.status,
    }


def _student_summary(row: Student) -> dict:
    return {
        "id": row.id,
        "school_student_code": row.school_student_code,
        "name": row.name,
        "is_active": row.is_active,
    }


def _enrollment_summary(row) -> dict:
    enrollment, class_code = row
    return {
        "id": enrollment.id,
        "student_id": enrollment.student_id,
        "administrative_class_id": enrollment.administrative_class_id,
        "administrative_class_code": class_code,
        "valid_from": enrollment.valid_from,
        "valid_to": enrollment.valid_to,
        "source": enrollment.source,
    }


async def list_academic_years(session, *, limit: int, offset: int) -> dict:
    rows = (
        await session.scalars(
            select(AcademicYear)
            .order_by(AcademicYear.start_date.desc(), AcademicYear.id)
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return _envelope(rows, limit, offset, _year_summary)


async def list_academic_terms(
    session, *, academic_year_id: UUID, limit: int, offset: int
) -> dict:
    rows = (
        await session.scalars(
            select(AcademicTerm)
            .where(AcademicTerm.academic_year_id == academic_year_id)
            .order_by(AcademicTerm.term_no, AcademicTerm.id)
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return _envelope(rows, limit, offset, _term_summary)


async def list_grade_cohorts(session, *, limit: int, offset: int) -> dict:
    rows = (
        await session.scalars(
            select(GradeCohort)
            .order_by(GradeCohort.intake_year.desc(), GradeCohort.code, GradeCohort.id)
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return _envelope(rows, limit, offset, _cohort_summary)


async def list_subjects(session, *, limit: int, offset: int) -> dict:
    rows = (
        await session.scalars(
            select(Subject).order_by(Subject.code, Subject.id).offset(offset).limit(limit + 1)
        )
    ).all()
    return _envelope(rows, limit, offset, _subject_summary)


async def list_administrative_classes(
    session,
    *,
    grade_cohort_id: UUID | None,
    status: str | None,
    limit: int,
    offset: int,
) -> dict:
    query = select(AdministrativeClass).order_by(
        AdministrativeClass.code, AdministrativeClass.id
    )
    if grade_cohort_id is not None:
        query = query.where(AdministrativeClass.grade_cohort_id == grade_cohort_id)
    if status is not None:
        query = query.where(AdministrativeClass.status == status)
    rows = (await session.scalars(query.offset(offset).limit(limit + 1))).all()
    return _envelope(rows, limit, offset, _class_summary)


async def list_students(
    session,
    *,
    class_id: UUID | None,
    query: str | None,
    active_only: bool,
    limit: int,
    offset: int,
) -> dict:
    statement = select(Student).order_by(Student.school_student_code, Student.id)
    if active_only:
        statement = statement.where(Student.is_active.is_(True))
    if class_id is not None:
        statement = statement.where(
            exists(
                select(1).where(
                    ClassEnrollment.student_id == Student.id,
                    ClassEnrollment.administrative_class_id == class_id,
                    current_enrollment(),
                )
            )
        )
    if query is not None:
        normalized = _normalize_query(query)
        if normalized:
            pattern = _like_pattern(normalized)
            statement = statement.where(
                or_(
                    Student.school_student_code.ilike(f"{pattern}%", escape="\\"),
                    Student.name.ilike(f"%{pattern}%", escape="\\"),
                )
            )
    rows = (await session.scalars(statement.offset(offset).limit(limit + 1))).all()
    return _envelope(rows, limit, offset, _student_summary)


async def list_student_enrollments(
    session, *, student_id: UUID, limit: int, offset: int
) -> dict:
    if await session.scalar(select(Student.id).where(Student.id == student_id)) is None:
        raise AccessError("resource_not_found_or_forbidden", 404)
    rows = (
        await session.execute(
            select(ClassEnrollment, AdministrativeClass.code)
            .join(
                AdministrativeClass,
                AdministrativeClass.id == ClassEnrollment.administrative_class_id,
            )
            .where(ClassEnrollment.student_id == student_id)
            .order_by(ClassEnrollment.valid_from.desc(), ClassEnrollment.created_at.desc())
            .offset(offset)
            .limit(limit + 1)
        )
    ).all()
    return _envelope(rows, limit, offset, _enrollment_summary)


async def create_academic_year(
    session: AsyncSession, principal: Principal, payload: AcademicYearCreate
) -> dict:
    async with operation(
        session,
        principal,
        payload.request_id,
        "academic_year.created",
        payload.model_dump(mode="json"),
    ) as op:
        await authorize(session, principal, "organization.manage")
        if op.replay is not None:
            return op.replay
        row = AcademicYear(
            code=payload.code,
            display_name=payload.display_name,
            start_date=payload.start_date,
            end_date=payload.end_date,
            status=payload.status,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as error:
            raise _conflict(error, {"uq_academic_years_code": CODE_CONFLICT}) from None
        op.result = {"id": str(row.id), "action": "academic_year.created"}
        return op.result


async def create_academic_term(
    session: AsyncSession, principal: Principal, payload: AcademicTermCreate
) -> dict:
    async with operation(
        session,
        principal,
        payload.request_id,
        "academic_term.created",
        payload.model_dump(mode="json"),
    ) as op:
        await authorize(session, principal, "organization.manage")
        if op.replay is not None:
            return op.replay
        year = await session.get(AcademicYear, payload.academic_year_id)
        if year is None:
            raise AccessError("academic_year_not_found", 404)
        if year.status == "closed":
            raise AccessError("academic_year_closed", 409)
        if payload.start_date < year.start_date or payload.end_date > year.end_date:
            raise AccessError("academic_term_outside_academic_year", 422)
        row = AcademicTerm(
            academic_year_id=payload.academic_year_id,
            term_no=payload.term_no,
            code=payload.code,
            start_date=payload.start_date,
            end_date=payload.end_date,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as error:
            raise _conflict(
                error,
                {
                    "uq_academic_terms_year_no": CODE_CONFLICT,
                    "uq_academic_terms_year_code": CODE_CONFLICT,
                    "ex_academic_terms_year_range": "academic_term_overlap",
                    "academic_term_outside_year": "academic_term_outside_academic_year",
                },
            ) from None
        op.result = {"id": str(row.id), "action": "academic_term.created"}
        return op.result


async def create_grade_cohort(
    session: AsyncSession, principal: Principal, payload: GradeCohortCreate
) -> dict:
    async with operation(
        session,
        principal,
        payload.request_id,
        "grade_cohort.created",
        payload.model_dump(mode="json"),
    ) as op:
        await authorize(session, principal, "organization.manage")
        if op.replay is not None:
            return op.replay
        row = GradeCohort(
            code=payload.code,
            display_name=payload.display_name,
            intake_year=payload.intake_year,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as error:
            raise _conflict(error, {"uq_grade_cohorts_code": CODE_CONFLICT}) from None
        op.result = {"id": str(row.id), "action": "grade_cohort.created"}
        return op.result


async def create_subject(
    session: AsyncSession, principal: Principal, payload: SubjectCreate
) -> dict:
    async with operation(
        session,
        principal,
        payload.request_id,
        "subject.created",
        payload.model_dump(mode="json"),
    ) as op:
        await authorize(session, principal, "organization.manage")
        if op.replay is not None:
            return op.replay
        row = Subject(code=payload.code, display_name=payload.display_name)
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as error:
            raise _conflict(error, {"uq_subjects_code": CODE_CONFLICT}) from None
        op.result = {"id": str(row.id), "action": "subject.created"}
        return op.result


async def create_administrative_class(
    session: AsyncSession, principal: Principal, payload: AdministrativeClassCreate
) -> dict:
    async with operation(
        session,
        principal,
        payload.request_id,
        "administrative_class.created",
        payload.model_dump(mode="json"),
    ) as op:
        await authorize(session, principal, "organization.manage")
        if op.replay is not None:
            return op.replay
        if await session.get(GradeCohort, payload.grade_cohort_id) is None:
            raise AccessError("grade_cohort_not_found", 404)
        row = AdministrativeClass(
            grade_cohort_id=payload.grade_cohort_id,
            code=payload.code,
            display_name=payload.display_name,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as error:
            raise _conflict(
                error, {"uq_administrative_classes_code": CODE_CONFLICT}
            ) from None
        op.result = {"id": str(row.id), "action": "administrative_class.created"}
        return op.result


async def transfer_student(
    session: AsyncSession, principal: Principal, payload: StudentTransferRequest
) -> dict:
    """Close the current homeroom enrollment and open a new one, keeping full history.

    The ``students`` row is locked FOR UPDATE so concurrent transfers of one student
    serialize. The PostgreSQL exclusion constraint on ``class_enrollments`` remains
    the final defense and maps to 409 rather than a raw database error.
    """
    async with operation(
        session,
        principal,
        payload.request_id,
        "student.transferred",
        payload.model_dump(mode="json"),
    ) as op:
        await authorize(session, principal, "organization.manage")
        if op.replay is not None:
            return op.replay
        student = await session.scalar(
            select(Student).where(Student.id == payload.student_id).with_for_update()
        )
        if student is None:
            raise AccessError("resource_not_found_or_forbidden", 404)
        if not student.is_active:
            raise AccessError("student_inactive", 409)
        target = await session.get(
            AdministrativeClass, payload.to_administrative_class_id
        )
        if target is None or target.status != "active":
            raise AccessError("administrative_class_unavailable", 409)
        current = await session.scalar(
            select(ClassEnrollment)
            .where(
                ClassEnrollment.student_id == student.id,
                current_enrollment(),
            )
            .order_by(
                ClassEnrollment.valid_from.desc(), ClassEnrollment.created_at.desc()
            )
            .limit(1)
            .with_for_update()
        )
        from_class_id: UUID | None = None
        if current is not None:
            if current.administrative_class_id == payload.to_administrative_class_id:
                raise AccessError("student_already_in_class", 409)
            if payload.effective_from <= current.valid_from:
                raise AccessError("transfer_effective_from_invalid", 409)
            current.valid_to = payload.effective_from
            try:
                await session.flush()
            except IntegrityError as error:
                raise _conflict(
                    error,
                    {"class_enrollment_history_immutable": "enrollment_overlap"},
                ) from None
            from_class_id = current.administrative_class_id
        overlap = await session.scalar(
            select(ClassEnrollment.id).where(
                ClassEnrollment.student_id == student.id,
                or_(
                    ClassEnrollment.valid_to.is_(None),
                    ClassEnrollment.valid_to > payload.effective_from,
                ),
            )
        )
        if overlap is not None:
            raise AccessError("enrollment_overlap", 409)
        row = ClassEnrollment(
            student_id=student.id,
            administrative_class_id=payload.to_administrative_class_id,
            valid_from=payload.effective_from,
            source="manual",
        )
        session.add(row)
        try:
            await session.flush()
        except (IntegrityError, DBAPIError) as error:
            if isinstance(error, IntegrityError):
                raise _conflict(
                    error,
                    {
                        "ex_class_enrollments_student_time": "enrollment_overlap",
                        "ck_class_enrollments_range": "enrollment_range_invalid",
                    },
                ) from None
            raise
        op.result = {
            "student_id": str(student.id),
            "from_administrative_class_id": (
                str(from_class_id) if from_class_id is not None else None
            ),
            "to_administrative_class_id": str(payload.to_administrative_class_id),
            "effective_from": payload.effective_from.isoformat(),
        }
        return op.result


__all__ = [
    "current_enrollment",
    "grade_label",
    "list_academic_terms",
    "list_academic_years",
    "list_administrative_classes",
    "list_grade_cohorts",
    "list_student_enrollments",
    "list_students",
    "list_subjects",
    "create_academic_term",
    "create_academic_year",
    "create_administrative_class",
    "create_grade_cohort",
    "create_subject",
    "transfer_student",
]
