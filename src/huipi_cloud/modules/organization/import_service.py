"""Two-phase trusted roster import: upload preview -> authorized confirmation.

Contract for B-1.1b:

``preview_student_import`` parses and validates the CSV without touching students or
enrollments, persists a frozen preview batch and returns row-level structured issues.
``confirm_student_import`` re-validates against current database state inside one audited
transaction and applies only if the whole batch is still valid; it never guesses identity
from names, never deletes students, never overwrites history and is request-idempotent.

Both write paths reuse ``operations.operation`` for audit, request-idempotency and the
single COMMIT; reads only expose counts and payload-free structured issues.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.organization import csv_import
from huipi_cloud.modules.organization.csv_import import (
    DUPLICATE_NAME_EXISTING,
    ENROLLMENT_MODES,
    MAX_FILE_BYTES,
    MAX_PREVIEW_ENTRIES,
    MAX_ROWS,
    NAME_MODES,
    ImportFileError,
    ParsedRow,
    normalize_name,
)
from huipi_cloud.modules.organization.models import (
    AcademicYear,
    AdministrativeClass,
    ClassEnrollment,
    Student,
    StudentImportBatch,
    StudentImportRow,
)
from huipi_cloud.modules.organization.operations import authorize, operation

PREVIEW_ACTION = "student_import.previewed"
CONFIRM_ACTION = "student_import.confirmed"
PREVIEW_PERMISSION = "student.import.preview"
CONFIRM_PERMISSION = "student.import.confirm"
FLUSH_CHUNK_SIZE = 500

# Stable row outcomes stored in the frozen preview and the confirmation result.
CREATE_STUDENT = "create_student"
CREATE_STUDENT_AND_ENROLLMENT = "create_student_and_enrollment"
CREATE_ENROLLMENT = "create_enrollment"
ENROLLMENT_ONLY_CREATE = "enrollment_only_create"
UNCHANGED = "unchanged"

CREATE_STUDENT_OUTCOMES = frozenset({CREATE_STUDENT, CREATE_STUDENT_AND_ENROLLMENT})
CREATE_ENROLLMENT_OUTCOMES = frozenset(
    {CREATE_STUDENT_AND_ENROLLMENT, CREATE_ENROLLMENT, ENROLLMENT_ONLY_CREATE}
)
EXISTING_STUDENT_OUTCOMES = frozenset({UNCHANGED, CREATE_ENROLLMENT, ENROLLMENT_ONLY_CREATE})

# Error codes deliberately remain stable and payload-free.
ACADEMIC_YEAR_NOT_FOUND = "academic_year_not_found"
ACADEMIC_YEAR_CLOSED = "academic_year_closed"
CLASS_CODE_NOT_FOUND = "class_code_not_found"
ADMINISTRATIVE_CLASS_INACTIVE = "administrative_class_inactive"
VALID_FROM_OUTSIDE_ACADEMIC_YEAR = "valid_from_outside_academic_year"
STUDENT_NAME_MISMATCH = "student_name_mismatch"
STUDENT_CODE_NOT_FOUND = "student_code_not_found"
STUDENT_CODE_CASE_AMBIGUOUS_EXISTING = "student_code_case_ambiguous_existing"
STUDENT_INACTIVE = "student_inactive"
ENROLLMENT_OVERLAP = "enrollment_overlap"
ENROLLMENT_CONFLICT_REQUIRES_TRANSFER = "enrollment_conflict_requires_transfer"
RESOURCE_NOT_FOUND = "resource_not_found_or_forbidden"
ALREADY_CONFIRMED = "student_import_already_confirmed"
IMPORT_CONFLICT = "student_import_conflict"
HAS_BLOCKING_ERRORS = "student_import_has_blocking_errors"
PREVIEW_STALE = "student_import_preview_stale"


@dataclass
class Evaluation:
    """Current database facts used while evaluating one frozen batch."""

    classes: dict[str, AdministrativeClass]
    students: dict[str, Student]


def _evaluate_row(
    row: ParsedRow,
    mode: str,
    academic_year: AcademicYear,
    classes: dict[str, AdministrativeClass],
    students: dict[str, Student],
    casefold_students: dict[str, Student],
) -> None:
    if mode in ENROLLMENT_MODES:
        class_row = classes.get(row.class_code) if row.class_code else None
        if class_row is None:
            row.add_error("class_code", CLASS_CODE_NOT_FOUND, "行政班不存在")
        elif class_row.status != "active":
            row.add_error("class_code", ADMINISTRATIVE_CLASS_INACTIVE, "行政班已停用")
        if row.valid_from is not None and not (
            academic_year.start_date <= row.valid_from <= academic_year.end_date
        ):
            row.add_error(
                "valid_from",
                VALID_FROM_OUTSIDE_ACADEMIC_YEAR,
                "生效日期不在学年范围内",
            )

    student = (
        students.get(row.normalized_student_code)
        if row.normalized_student_code is not None
        else None
    )
    if student is None and row.normalized_student_code is not None:
        # A pre-existing student whose code differs only by letter case is most likely
        # the same person, but guessing either identity is unsafe. Never create a second
        # identity and never silently reuse/merge the existing one; block the row for a
        # human decision in every mode.
        if casefold_students.get(row.normalized_student_code.casefold()) is not None:
            row.add_error(
                "student_code",
                STUDENT_CODE_CASE_AMBIGUOUS_EXISTING,
                "学号与已有学生仅大小写不同，需人工确认",
            )
            return
    if mode == "enrollment_only":
        if student is None:
            row.add_error("student_code", STUDENT_CODE_NOT_FOUND, "学号对应的学生不存在")
        elif not student.is_active:
            row.add_error("student_code", STUDENT_INACTIVE, "学生已停用")
        return
    if student is None:
        row.outcome = (
            CREATE_STUDENT if mode == "student_only" else CREATE_STUDENT_AND_ENROLLMENT
        )
        return
    if not student.is_active:
        row.add_error("student_code", STUDENT_INACTIVE, "学生已停用")
        return
    if normalize_name(student.name) != row.name:
        row.add_error("name", STUDENT_NAME_MISMATCH, "姓名与已有学生记录不一致")
        return
    row.outcome = UNCHANGED if mode == "student_only" else None


def _range_overlaps(enrollment: ClassEnrollment, valid_from: date) -> bool:
    """An imported enrollment is half-open ``[valid_from, +infinity)``.

    It overlaps an existing half-open range when the existing range has no end or ends
    after the imported start. Future-dated existing enrollments therefore count too: the
    import never closes them and PostgreSQL's exclusion constraint is only the last line
    of defense, not the first place a duplicate is discovered.
    """
    return enrollment.valid_to is None or enrollment.valid_to > valid_from


async def _evaluate_enrollment_rows(
    session: AsyncSession,
    mode: str,
    rows: list[ParsedRow],
    classes: dict[str, AdministrativeClass],
    students: dict[str, Student],
) -> None:
    if mode not in ENROLLMENT_MODES:
        return
    pending: list[tuple[ParsedRow, Student, AdministrativeClass]] = []
    for row in rows:
        if (
            row.errors
            or row.normalized_student_code is None
            or row.class_code is None
            or row.valid_from is None
        ):
            continue
        student = students.get(row.normalized_student_code)
        class_row = classes.get(row.class_code)
        if student is None or class_row is None:
            continue
        pending.append((row, student, class_row))
    if not pending:
        return
    student_ids = {student.id for _, student, _ in pending}
    # Query every possibly-overlapping enrollment for the batch: any range that starts
    # before the earliest imported start and does not end before it may overlap a later
    # row, including future-dated enrollments the "current class" predicate would skip.
    min_valid_from = min(row.valid_from for row, _, _ in pending)
    existing = (
        await session.scalars(
            select(ClassEnrollment)
            .where(
                ClassEnrollment.student_id.in_(student_ids),
                or_(
                    ClassEnrollment.valid_to.is_(None),
                    ClassEnrollment.valid_to > min_valid_from,
                ),
            )
            .order_by(ClassEnrollment.valid_from)
        )
    ).all()
    by_student: dict[UUID, list[ClassEnrollment]] = {}
    for enrollment in existing:
        by_student.setdefault(enrollment.student_id, []).append(enrollment)

    for row, student, class_row in pending:
        enrollments = [
            enrollment
            for enrollment in by_student.get(student.id, [])
            if _range_overlaps(enrollment, row.valid_from)
        ]
        same_class = [
            enrollment
            for enrollment in enrollments
            if enrollment.administrative_class_id == class_row.id
        ]
        identical = [
            enrollment
            for enrollment in same_class
            if enrollment.valid_from == row.valid_from and enrollment.valid_to is None
        ]
        if identical:
            row.outcome = UNCHANGED
        elif same_class:
            row.add_error("class_code", ENROLLMENT_OVERLAP, "该班级已存在时间重叠的归属")
        elif enrollments:
            row.add_error(
                "class_code",
                ENROLLMENT_CONFLICT_REQUIRES_TRANSFER,
                "学生已存在其他班级归属（当前或未来生效），需先办理转班",
            )
        elif mode == "enrollment_only":
            row.outcome = ENROLLMENT_ONLY_CREATE
        else:
            row.outcome = CREATE_ENROLLMENT


async def _mark_duplicate_name_warnings(
    session: AsyncSession, mode: str, rows: list[ParsedRow]
) -> None:
    if mode not in NAME_MODES:
        return
    message = "存在同名但不同学号的学生，请人工确认"
    in_batch: dict[str, list[ParsedRow]] = {}
    for row in rows:
        if row.name is None or row.errors:
            continue
        in_batch.setdefault(row.name, []).append(row)
    for group in in_batch.values():
        codes = {row.normalized_student_code for row in group}
        if len(codes) > 1:
            for row in group:
                row.add_warning("name", DUPLICATE_NAME_EXISTING, message)

    new_students = [
        row
        for row in rows
        if row.name is not None
        and not row.errors
        and row.outcome in CREATE_STUDENT_OUTCOMES
    ]
    if not new_students:
        return
    names = {row.name for row in new_students}
    existing_names = {
        normalize_name(name)
        for name in await session.scalars(select(Student.name).where(Student.name.in_(names)))
    }
    for row in new_students:
        if row.name in existing_names and not any(
            warning.code == DUPLICATE_NAME_EXISTING for warning in row.warnings
        ):
            row.add_warning("name", DUPLICATE_NAME_EXISTING, message)


async def _evaluate_rows(
    session: AsyncSession,
    *,
    mode: str,
    academic_year: AcademicYear,
    rows: list[ParsedRow],
) -> Evaluation:
    """Evaluate every format-valid row against current database state.

    Preview persists the result; confirmation recomputes it and rejects the whole batch
    when any outcome or blocking issue differs from the frozen preview.
    """
    candidates = [row for row in rows if not row.errors]
    class_codes = {
        row.class_code
        for row in candidates
        if mode in ENROLLMENT_MODES and row.class_code is not None
    }
    classes: dict[str, AdministrativeClass] = {}
    if class_codes:
        classes = {
            class_row.code: class_row
            for class_row in await session.scalars(
                select(AdministrativeClass).where(
                    AdministrativeClass.code.in_(class_codes)
                )
            )
        }
    student_codes = {
        row.normalized_student_code
        for row in candidates
        if row.normalized_student_code is not None
    }
    students: dict[str, Student] = {}
    casefold_students: dict[str, Student] = {}
    if student_codes:
        students = {
            student.school_student_code: student
            for student in await session.scalars(
                select(Student).where(Student.school_student_code.in_(student_codes))
            )
        }
        # Codes are restricted to ASCII by CODE_PATTERN, so SQL lower() and Python
        # casefold() agree. One batched query, no per-row lookups.
        casefold_codes = {code.casefold() for code in student_codes}
        casefold_students = {
            student.school_student_code.casefold(): student
            for student in await session.scalars(
                select(Student).where(
                    func.lower(Student.school_student_code).in_(casefold_codes)
                )
            )
        }

    for row in candidates:
        _evaluate_row(
            row, mode, academic_year, classes, students, casefold_students
        )
    await _evaluate_enrollment_rows(session, mode, rows, classes, students)
    await _mark_duplicate_name_warnings(session, mode, rows)
    for row in rows:
        if row.errors:
            row.outcome = None
    return Evaluation(classes=classes, students=students)


def _counts(rows) -> dict[str, int]:
    counts = {
        "row_total": len(rows),
        "error_rows": 0,
        "warning_rows": 0,
        "valid_rows": 0,
        "create_students": 0,
        "existing_students": 0,
        "create_enrollments": 0,
        "unchanged_rows": 0,
    }
    for row in rows:
        counts[f"{row.row_state}_rows"] += 1
        outcome = row.outcome
        if outcome in CREATE_STUDENT_OUTCOMES:
            counts["create_students"] += 1
        if outcome in EXISTING_STUDENT_OUTCOMES:
            counts["existing_students"] += 1
        if outcome in CREATE_ENROLLMENT_OUTCOMES:
            counts["create_enrollments"] += 1
        if outcome == UNCHANGED:
            counts["unchanged_rows"] += 1
    return counts


def _issue_entry(row_number: int, issue) -> dict:
    if isinstance(issue, dict):
        return {
            "row_number": row_number,
            "field": issue.get("field"),
            "code": issue["code"],
            "message": issue["message"],
        }
    return {
        "row_number": row_number,
        "field": issue.field,
        "code": issue.code,
        "message": issue.message,
    }


def _aggregate_issues(rows) -> tuple[list[dict], list[dict], bool]:
    errors: list[dict] = []
    warnings: list[dict] = []
    for row in rows:
        errors.extend(_issue_entry(row.row_number, issue) for issue in row.errors)
        warnings.extend(_issue_entry(row.row_number, issue) for issue in row.warnings)
    truncated = (
        len(errors) > MAX_PREVIEW_ENTRIES or len(warnings) > MAX_PREVIEW_ENTRIES
    )
    return errors[:MAX_PREVIEW_ENTRIES], warnings[:MAX_PREVIEW_ENTRIES], truncated


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _preview_result(batch: StudentImportBatch, rows) -> dict:
    counts = dict(batch.preview_counts or {})
    errors, warnings, truncated = _aggregate_issues(rows)
    return {
        "batch_id": str(batch.id),
        "mode": batch.mode,
        "status": batch.status,
        "input_sha256": batch.input_sha256,
        "file_size_bytes": batch.file_size_bytes,
        "row_count": batch.row_count,
        "academic_year_id": str(batch.academic_year_id),
        "counts": counts,
        "can_confirm": batch.status == "previewed" and counts.get("error_rows", 0) == 0,
        "errors": errors,
        "warnings": warnings,
        "truncated": truncated,
        "created_at": _iso(batch.created_at),
    }


def _row_model(batch_id: UUID, row: ParsedRow) -> StudentImportRow:
    return StudentImportRow(
        id=uuid4(),
        batch_id=batch_id,
        row_number=row.row_number,
        student_code=row.student_code,
        normalized_student_code=row.normalized_student_code,
        name=row.name,
        class_code=row.class_code,
        valid_from=row.valid_from,
        row_state=row.row_state,
        outcome=row.outcome,
        errors=[issue.as_dict() for issue in row.errors],
        warnings=[issue.as_dict() for issue in row.warnings],
    )


def _parsed_row(row: StudentImportRow) -> ParsedRow:
    return ParsedRow(
        row_number=row.row_number,
        student_code=row.student_code,
        normalized_student_code=row.normalized_student_code,
        name=row.name,
        class_code=row.class_code,
        valid_from=row.valid_from,
    )


async def preview_student_import(
    session: AsyncSession,
    principal: Principal,
    request_id: UUID,
    *,
    mode: str,
    academic_year_id: UUID,
    filename: str | None,
    content: bytes,
) -> dict:
    """Freeze a parsed preview batch; never writes students or enrollments."""
    input_sha256 = hashlib.sha256(content).hexdigest()
    payload = {
        "mode": mode,
        "academic_year_id": str(academic_year_id),
        "filename": filename,
        "input_sha256": input_sha256,
        "file_size_bytes": len(content),
    }
    async with operation(
        session, principal, request_id, PREVIEW_ACTION, payload
    ) as op:
        await authorize(session, principal, PREVIEW_PERMISSION)
        if op.replay is not None:
            return op.replay
        academic_year = await session.get(AcademicYear, academic_year_id)
        if academic_year is None:
            raise AccessError(ACADEMIC_YEAR_NOT_FOUND, 404)
        if academic_year.status == "closed":
            raise AccessError(ACADEMIC_YEAR_CLOSED, 409)
        try:
            parsed = csv_import.parse_student_import_file(mode, content)
        except ImportFileError as error:
            raise AccessError(error.code, error.status_code) from None

        await _evaluate_rows(
            session, mode=mode, academic_year=academic_year, rows=parsed.rows
        )
        counts = _counts(parsed.rows)
        batch = StudentImportBatch(
            id=uuid4(),
            mode=mode,
            status="previewed",
            input_sha256=input_sha256,
            file_size_bytes=len(content),
            row_count=len(parsed.rows),
            academic_year_id=academic_year_id,
            requested_by_user_id=principal.user_id,
            preview_counts=counts,
        )
        session.add(batch)
        await session.flush()
        row_models = [_row_model(batch.id, row) for row in parsed.rows]
        for start in range(0, len(row_models), FLUSH_CHUNK_SIZE):
            session.add_all(row_models[start : start + FLUSH_CHUNK_SIZE])
            await session.flush()
        result = _preview_result(batch, row_models)
        op.result = result
        return result


async def confirm_student_import(
    session: AsyncSession,
    principal: Principal,
    request_id: UUID,
    *,
    batch_id: UUID,
    expected_input_sha256: str | None,
) -> dict:
    """Re-validate a frozen batch against current state and apply it atomically."""
    payload = {
        "batch_id": str(batch_id),
        "expected_input_sha256": expected_input_sha256,
    }
    async with operation(
        session, principal, request_id, CONFIRM_ACTION, payload
    ) as op:
        await authorize(session, principal, CONFIRM_PERMISSION)
        if op.replay is not None:
            return op.replay
        batch = await session.scalar(
            select(StudentImportBatch)
            .where(StudentImportBatch.id == batch_id)
            .with_for_update()
        )
        if batch is None:
            raise AccessError(RESOURCE_NOT_FOUND, 404)
        if batch.status == "confirmed":
            raise AccessError(ALREADY_CONFIRMED, 409)
        if (
            expected_input_sha256 is not None
            and expected_input_sha256 != batch.input_sha256
        ):
            raise AccessError(IMPORT_CONFLICT, 409)

        stored_rows = list(
            await session.scalars(
                select(StudentImportRow)
                .where(StudentImportRow.batch_id == batch.id)
                .order_by(StudentImportRow.row_number)
            )
        )
        if not stored_rows:
            raise AccessError(PREVIEW_STALE, 409)
        if any(row.row_state == "error" for row in stored_rows):
            raise AccessError(HAS_BLOCKING_ERRORS, 409)
        academic_year = await session.get(AcademicYear, batch.academic_year_id)
        if academic_year is None or academic_year.status == "closed":
            raise AccessError(PREVIEW_STALE, 409)

        rows = [_parsed_row(row) for row in stored_rows]
        evaluation = await _evaluate_rows(
            session, mode=batch.mode, academic_year=academic_year, rows=rows
        )
        for stored, row in zip(stored_rows, rows, strict=True):
            if row.errors or row.outcome != stored.outcome:
                raise AccessError(PREVIEW_STALE, 409)

        student_ids: dict[str, UUID] = {
            code: student.id for code, student in evaluation.students.items()
        }
        new_students: list[Student] = []
        for row in rows:
            if row.outcome in CREATE_STUDENT_OUTCOMES:
                student_id = uuid4()
                student_ids[row.normalized_student_code] = student_id
                new_students.append(
                    Student(
                        id=student_id,
                        school_student_code=row.normalized_student_code,
                        name=row.name,
                        is_active=True,
                    )
                )
        enrollments: list[ClassEnrollment] = []
        unchanged_rows = 0
        for row in rows:
            if row.outcome == UNCHANGED:
                unchanged_rows += 1
            if row.outcome not in CREATE_ENROLLMENT_OUTCOMES:
                continue
            class_row = evaluation.classes.get(row.class_code) if row.class_code else None
            student_id = student_ids.get(row.normalized_student_code)
            if class_row is None or student_id is None or row.valid_from is None:
                raise AccessError(PREVIEW_STALE, 409)
            enrollments.append(
                ClassEnrollment(
                    id=uuid4(),
                    student_id=student_id,
                    administrative_class_id=class_row.id,
                    valid_from=row.valid_from,
                    source="student_import",
                    source_import_batch_id=batch.id,
                )
            )

        try:
            for start in range(0, len(new_students), FLUSH_CHUNK_SIZE):
                session.add_all(new_students[start : start + FLUSH_CHUNK_SIZE])
                await session.flush()
            for start in range(0, len(enrollments), FLUSH_CHUNK_SIZE):
                session.add_all(enrollments[start : start + FLUSH_CHUNK_SIZE])
                await session.flush()
        except IntegrityError:
            raise AccessError(PREVIEW_STALE, 409) from None

        confirmed_at = datetime.now(UTC)
        result = {
            "batch_id": str(batch.id),
            "status": "confirmed",
            "created_students": len(new_students),
            "created_enrollments": len(enrollments),
            "unchanged_rows": unchanged_rows,
            "rejected_rows": 0,
            "confirmed_at": confirmed_at.isoformat(),
            "replayed": False,
        }
        batch.status = "confirmed"
        batch.confirmed_by_user_id = principal.user_id
        batch.confirmed_at = confirmed_at
        batch.confirm_result = result
        try:
            await session.flush()
        except IntegrityError:
            raise AccessError(PREVIEW_STALE, 409) from None
        op.result = result
        return result


async def get_student_import(session: AsyncSession, *, batch_id: UUID) -> dict:
    batch = await session.get(StudentImportBatch, batch_id)
    if batch is None:
        raise AccessError(RESOURCE_NOT_FOUND, 404)
    rows = list(
        await session.scalars(
            select(StudentImportRow)
            .where(StudentImportRow.batch_id == batch.id)
            .order_by(StudentImportRow.row_number)
        )
    )
    return _preview_result(batch, rows)


async def list_student_imports(
    session: AsyncSession, *, limit: int, offset: int
) -> dict:
    rows = list(
        await session.scalars(
            select(StudentImportBatch)
            .order_by(StudentImportBatch.created_at.desc(), StudentImportBatch.id)
            .offset(offset)
            .limit(limit + 1)
        )
    )
    has_next = len(rows) > limit
    records = [
        {
            "batch_id": str(row.id),
            "mode": row.mode,
            "status": row.status,
            "input_sha256": row.input_sha256,
            "row_count": row.row_count,
            "academic_year_id": str(row.academic_year_id),
            "counts": dict(row.preview_counts or {}),
            "created_at": _iso(row.created_at),
            "confirmed_at": _iso(row.confirmed_at),
        }
        for row in rows[:limit]
    ]
    return {
        "records": records,
        "limit": limit,
        "offset": offset,
        "has_next": has_next,
        "next_offset": offset + limit if has_next else None,
    }


__all__ = [
    "ACADEMIC_YEAR_CLOSED",
    "ACADEMIC_YEAR_NOT_FOUND",
    "ADMINISTRATIVE_CLASS_INACTIVE",
    "ALREADY_CONFIRMED",
    "CLASS_CODE_NOT_FOUND",
    "CONFIRM_ACTION",
    "CONFIRM_PERMISSION",
    "ENROLLMENT_CONFLICT_REQUIRES_TRANSFER",
    "ENROLLMENT_OVERLAP",
    "HAS_BLOCKING_ERRORS",
    "IMPORT_CONFLICT",
    "MAX_FILE_BYTES",
    "MAX_PREVIEW_ENTRIES",
    "MAX_ROWS",
    "PREVIEW_ACTION",
    "PREVIEW_PERMISSION",
    "PREVIEW_STALE",
    "STUDENT_CODE_CASE_AMBIGUOUS_EXISTING",
    "STUDENT_CODE_NOT_FOUND",
    "STUDENT_INACTIVE",
    "STUDENT_NAME_MISMATCH",
    "VALID_FROM_OUTSIDE_ACADEMIC_YEAR",
    "confirm_student_import",
    "get_student_import",
    "list_student_imports",
    "preview_student_import",
]
