"""Versioned rosters with current database authority and historical identity snapshots."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.exams.models import ExamEvent, ExamSession, SessionPaper
from huipi_cloud.modules.identity.models import User
from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.organization.models import AdministrativeClass, ClassEnrollment, Student
from huipi_cloud.modules.papers.models import PaperVersion
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityGrant

from .models import ExamParticipation, RosterEntrySnapshot, RosterRevision
from .operations import operation
from .permissions import ROSTER_OPERATOR_ROLE
from .schemas import MAX_ENTRIES

R, E, P = (m.__table__ for m in (RosterRevision, RosterEntrySnapshot, ExamParticipation))
S, EV, B, V = (m.__table__ for m in (ExamSession, ExamEvent, SessionPaper, PaperVersion))
MAX_CONTENT_BYTES = 1024 * 1024


async def authorize(db, principal, session_id, *actions):
    await exams.authorize(
        db, principal, "exam_session", session_id, {f"exam.roster.{a}" for a in actions}
    )


async def locked_session(db, identifier):
    value = await exams.row(db, S, identifier)
    event = await exams.row(db, EV, value["exam_event_id"], share=True)
    value = await exams.row(db, S, identifier, lock=True)
    if event["lifecycle"] not in {"draft", "active"} or value["lifecycle"] not in {
        "draft",
        "ready",
    }:
        raise AccessError("roster_session_closed", 409)
    return value


async def locked_revision(db, principal, request, *actions):
    value = await exams.row(db, R, request.roster_revision_id)
    await authorize(db, principal, value["exam_session_id"], *actions)
    session = await locked_session(db, value["exam_session_id"])
    value = await exams.row(db, R, request.roster_revision_id, lock=True)
    exams.revision(value, request.expected_revision)
    return session, value


# 250 * 16 snapshot columns stays far below asyncpg's 32767 bind limit.
WRITE_BATCH_SIZE = 250
ST, CE, CL = (m.__table__ for m in (Student, ClassEnrollment, AdministrativeClass))


async def locked_sources(db, table, identifiers, *conditions):
    """Fresh Core rows; one bounded query, stable lock order within each table."""
    if not identifiers:
        return {}
    rows = (
        await db.execute(
            select(table)
            .where(table.c.id.in_(sorted(identifiers)), *conditions)
            .order_by(table.c.id)
            .with_for_update(read=True)
        )
    ).mappings()
    return {r["id"]: r for r in rows}


async def insert_batches(db, table, values):
    # Explicit multi-VALUES: not an executemany loop disguised as one client event.
    # Database row triggers, constraints and transaction semantics all remain active.
    for start in range(0, len(values), WRITE_BATCH_SIZE):
        await db.execute(insert(table).values(values[start : start + WRITE_BATCH_SIZE]))


def snapshot_rows(roster, selections, students, by_student, classrooms, participation_ids):
    """Pure, bounded assembly after source locks; no database work or authority decisions."""
    new_participations, snapshots = [], []
    for selection in selections:
        student = students[selection.student_id]
        enrollment = by_student[student["id"]]
        classroom = classrooms[enrollment["administrative_class_id"]]
        if student["id"] not in participation_ids:
            participation_ids[student["id"]] = uuid4()
            new_participations.append(
                dict(
                    id=participation_ids[student["id"]],
                    exam_session_id=roster["exam_session_id"],
                    student_id=student["id"],
                )
            )
        snapshots.append(
            dict(
                id=uuid4(),
                roster_revision_id=roster["id"],
                exam_session_id=roster["exam_session_id"],
                participation_id=participation_ids[student["id"]],
                student_id=student["id"],
                enrollment_id=enrollment["id"],
                administrative_class_id=classroom["id"],
                student_name=student["name"],
                student_code=student["school_student_code"],
                class_name=classroom["display_name"],
                class_code=classroom["code"],
                exam_number=selection.exam_number,
                session_paper_id=selection.session_paper_id,
            )
        )
    return new_participations, snapshots


async def materialize(db, roster, entries):
    """Lock trusted sources once, then capture the complete immutable snapshot.

    The caller already holds Event SHARE and Session UPDATE (and existing Roster
    UPDATE). Same-session writers serialize before participation lookup. All shared
    source locks are retained through audit and COMMIT; no cache survives a call.
    """
    if len(entries) > MAX_ENTRIES:
        raise AccessError("roster_entry_limit", 422)
    if not entries:
        return
    selections = sorted(entries, key=lambda e: e.student_id)
    student_ids = [e.student_id for e in selections]
    students = await locked_sources(db, ST, student_ids)
    if len(students) != len(student_ids) or any(not s["is_active"] for s in students.values()):
        raise AccessError("roster_student_unavailable", 409)
    # Match freeze's student -> enrollment lock order, with globally ordered enrollment IDs.
    enrollments = (
        (
            await db.execute(
                select(CE)
                .where(
                    CE.c.student_id.in_(student_ids),
                    CE.c.valid_from <= roster["business_date"],
                    CE.c.valid_to.is_(None) | (CE.c.valid_to > roster["business_date"]),
                )
                .order_by(CE.c.id)
                .with_for_update(read=True)
            )
        )
        .mappings()
        .all()
    )
    by_student = {r["student_id"]: r for r in enrollments}
    if len(enrollments) != len(student_ids) or len(by_student) != len(student_ids):
        raise AccessError("roster_enrollment_missing", 409)
    classrooms = await locked_sources(db, CL, {r["administrative_class_id"] for r in enrollments})
    binding_ids = {e.session_paper_id for e in selections if e.session_paper_id is not None}
    bindings = await locked_sources(db, B, binding_ids)
    if len(bindings) != len(binding_ids):
        raise AccessError(exams.DENIED, 404)
    if any(
        b["exam_session_id"] != roster["exam_session_id"] or b["status"] != "active"
        for b in bindings.values()
    ):
        raise AccessError("roster_paper_invalid", 409)
    paper_ids = {b["paper_version_id"] for b in bindings.values()}
    papers = await locked_sources(db, V, paper_ids)
    if len(papers) != len(paper_ids) or any(p["status"] != "locked" for p in papers.values()):
        raise AccessError("roster_paper_unavailable", 409)
    existing = (
        await db.execute(
            select(P.c.student_id, P.c.id).where(
                P.c.exam_session_id == roster["exam_session_id"], P.c.student_id.in_(student_ids)
            )
        )
    ).all()
    new_participations, snapshots = snapshot_rows(
        roster, selections, students, by_student, classrooms, dict(existing)
    )
    # No ON CONFLICT DO NOTHING: an unexpected identity conflict rolls back the whole
    # request; immutable participation must never silently point at a different UUID.
    await insert_batches(db, P, new_participations)
    await insert_batches(db, E, snapshots)


async def new_revision(db, principal, session, business_date, reason, entries, previous=None):
    if business_date != session["starts_at"].astimezone(exams.SCHOOL_ZONE).date():
        raise AccessError("roster_business_date_mismatch", 409)
    count = await db.scalar(
        select(func.count()).select_from(R).where(R.c.exam_session_id == session["id"])
    )
    if count >= 100:
        raise AccessError("roster_version_limit", 409)
    identifier = uuid4()
    await db.execute(
        insert(R).values(
            id=identifier,
            exam_session_id=session["id"],
            version_no=count + 1,
            previous_revision_id=previous,
            business_date=business_date,
            reason=reason,
            created_by_user_id=principal.user_id,
        )
    )
    value = await exams.row(db, R, identifier)
    await materialize(db, value, entries)
    return exams.public(value)


async def create_roster(db, principal, request):
    async with operation(
        db, principal, request.request_id, "create", request.model_dump(mode="json")
    ) as op:
        await authorize(db, principal, request.exam_session_id, "manage")
        if op.replay is not None:
            return op.replay
        session = await locked_session(db, request.exam_session_id)
        if await db.scalar(select(R.c.id).where(R.c.exam_session_id == session["id"])):
            raise AccessError("roster_requires_correction", 409)
        op.result = await new_revision(
            db, principal, session, request.business_date, request.reason, request.entries
        )
        return op.result


async def replace_entries(db, principal, request):
    async with operation(
        db, principal, request.request_id, "replace", request.model_dump(mode="json")
    ) as op:
        value = await exams.row(db, R, request.roster_revision_id)
        await authorize(
            db,
            principal,
            value["exam_session_id"],
            "manage",
            *(["correct"] if value["previous_revision_id"] else []),
        )
        if op.replay is not None:
            return op.replay
        _, value = await locked_revision(
            db,
            principal,
            request,
            "manage",
            *(["correct"] if value["previous_revision_id"] else []),
        )
        if value["status"] != "draft":
            raise AccessError("roster_frozen", 409)
        await db.execute(delete(E).where(E.c.roster_revision_id == value["id"]))
        await materialize(db, value, request.entries)
        op.result = await exams.bump(db, R, value)
        return op.result


async def correct_roster(db, principal, request):
    async with operation(
        db, principal, request.request_id, "correct", request.model_dump(mode="json")
    ) as op:
        value = await exams.row(db, R, request.roster_revision_id)
        await authorize(db, principal, value["exam_session_id"], "manage", "correct")
        if op.replay is not None:
            return op.replay
        session, value = await locked_revision(db, principal, request, "manage", "correct")
        if value["status"] != "frozen" or not value["is_current"]:
            raise AccessError("roster_not_current", 409)
        op.result = await new_revision(
            db,
            principal,
            session,
            value["business_date"],
            request.reason,
            request.entries,
            value["id"],
        )
        return op.result


async def freeze_roster(db, principal, request):
    async with operation(
        db, principal, request.request_id, "freeze", request.model_dump(mode="json")
    ) as op:
        value = await exams.row(db, R, request.roster_revision_id)
        await authorize(db, principal, value["exam_session_id"], "freeze")
        if op.replay is not None:
            return op.replay
        session, value = await locked_revision(db, principal, request, "freeze")
        if value["status"] != "draft":
            raise AccessError("roster_frozen", 409)
        if session["lifecycle"] != "ready":
            raise AccessError("roster_session_not_ready", 409)
        # Trigger validates numbers, bindings, current student/enrollment and paper state.
        if value["previous_revision_id"]:
            await db.execute(
                update(R).where(R.c.id == value["previous_revision_id"]).values(is_current=False)
            )
        result = await db.execute(
            update(R)
            .where(R.c.id == value["id"])
            .values(
                status="frozen",
                is_current=True,
                revision=value["revision"] + 1,
                frozen_by_user_id=principal.user_id,
                frozen_at=func.clock_timestamp(),
            )
            .returning(R)
        )
        op.result = exams.public(result.mappings().one())
        return op.result


async def inspect_roster(db, principal, identifier, *, offset=0, limit=100):
    if offset < 0 or not 1 <= limit <= 100:
        raise AccessError("roster_page_invalid", 422)
    try:
        async with db.begin():
            value = await exams.row(db, R, identifier)
            await authorize(db, principal, value["exam_session_id"], "read")
            value = await exams.row(db, R, identifier, share=True)
            rows = list(
                (
                    await db.execute(
                        select(E)
                        .where(E.c.roster_revision_id == identifier)
                        .order_by(E.c.student_code, E.c.id)
                        .offset(offset)
                        .limit(limit + 1)
                    )
                ).mappings()
            )
            count = await db.scalar(
                select(func.count()).select_from(E).where(E.c.roster_revision_id == identifier)
            )
            unknown = await db.scalar(
                select(func.count())
                .select_from(E)
                .where(E.c.roster_revision_id == identifier, E.c.session_paper_id.is_(None))
            )
            return {
                "roster": exams.public(value),
                "entries": [exams.public(r) for r in rows[:limit]],
                "total": count,
                "offset": offset,
                "limit": limit,
                "truncated": len(rows) > limit,
                "unknown_variant_count": unknown,
                "attendance_status": "not_recorded",
            }
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError("roster_operation_unavailable", 503) from None


async def history(db, principal, session_id):
    try:
        async with db.begin():
            await authorize(db, principal, session_id, "read")
            await exams.row(db, S, session_id, share=True)
            rows = (
                await db.execute(
                    select(R).where(R.c.exam_session_id == session_id).order_by(R.c.version_no)
                )
            ).mappings()
            return {"revisions": [exams.public(r) for r in rows]}
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError("roster_operation_unavailable", 503) from None


async def enable_operator(db, actor_user_id, target_user_id, request_id, approval_ref):
    if not approval_ref.strip() or len(approval_ref) > 120:
        raise AccessError("roster_approval_required", 422)
    user = await db.get(User, actor_user_id)
    if user is None:
        raise AccessError(exams.DENIED, 404)
    principal = Principal(
        user_id=user.id,
        issuer=user.issuer,
        subject=user.subject,
        is_admin=user.is_admin,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await db.rollback()
    payload = dict(target=str(target_user_id), approval_ref=approval_ref)
    async with operation(db, principal, request_id, "enable_operator", payload) as op:
        scope = await security.installation_scope(db)
        await security.require_permission(db, principal, "security.grants.manage", scope.id)
        if op.replay is not None:
            return op.replay
        target = await db.scalar(
            select(User).where(User.id == target_user_id).with_for_update(read=True)
        )
        if target is None or not target.is_active:
            raise AccessError(exams.DENIED, 404)
        identifier = uuid4()
        await db.execute(
            insert(SecurityGrant).values(
                id=identifier,
                user_id=target.id,
                role_version_id=ROSTER_OPERATOR_ROLE,
                scope_id=scope.id,
            )
        )
        op.result = {"grant_id": str(identifier)}
        return op.result
