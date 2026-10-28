"""A class restriction cannot combine permissions from separate Grants."""

from uuid import uuid4

from sqlalchemy import insert, select, update

from huipi_cloud.modules.exam_rosters.models import RosterEntrySnapshot, RosterRevision
from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityGrant

from .models import ExamResultClassAccess

C = ExamResultClassAccess.__table__
G = SecurityGrant.__table__
R, E = RosterRevision.__table__, RosterEntrySnapshot.__table__
REQUIRED = {"exam.mark.read", "exam.roster.read", "exam.script.read", "exam.identity.verify"}
DENIED = "resource_not_found_or_forbidden"


async def covered(db, grant, sid, exporting=False, extra_actions=frozenset()):
    scope = await security.scope(db, grant["scope_id"])
    codes = await security.role_codes(db, grant["role_version_id"], scope.kind)
    required = REQUIRED | ({"exam.score.review"} if exporting else set()) | set(extra_actions)
    session = await exams.row(db, exams.S, sid)
    matches = (
        scope.kind == "installation"
        or scope.kind == "exam_session"
        and scope.exam_session_id == sid
        or scope.kind == "exam_event"
        and scope.exam_event_id == session["exam_event_id"]
    )
    return matches and required <= codes


async def authorize(db, p, sid, class_id=None, *, exporting=False, extra_actions=frozenset()):
    await identity.refresh_principal(db, p, lock=True)
    rows = (
        (
            await db.execute(
                select(C, G.c.scope_id, G.c.role_version_id)
                .join(G, G.c.id == C.c.grant_id)
                .where(
                    C.c.exam_session_id == sid,
                    C.c.revoked_at.is_(None),
                    G.c.user_id == p.user_id,
                    G.c.revoked_at.is_(None),
                )
                .order_by(C.c.id)
                .limit(101)
                .with_for_update(read=True, of=[C, G])
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > 100:
        raise AccessError("result_access_limit", 413)
    allowed = set()
    for row in rows:
        if (not exporting or row["export_allowed"]) and await covered(
            db, row, sid, exporting, extra_actions
        ):
            allowed.add(row["administrative_class_id"])
    if not allowed or class_id is not None and class_id not in allowed:
        raise AccessError(DENIED, 404)
    return {class_id} if class_id else allowed


async def grant_class(db, p, request_id, *, grant_id, session_id, class_id, export_allowed=False):
    payload = dict(
        type="exam_result_class",
        grant_id=str(grant_id),
        session_id=str(session_id),
        class_id=str(class_id),
        export_allowed=export_allowed,
    )
    async with security.operation(db, p, request_id, "grant", payload) as op:
        grant = (
            (
                await db.execute(
                    select(G)
                    .where(G.c.id == grant_id, G.c.revoked_at.is_(None))
                    .with_for_update(read=True)
                )
            )
            .mappings()
            .first()
        )
        if grant is None or grant["user_id"] == p.user_id:
            raise AccessError(DENIED, 404)
        scope = await security.scope(db, grant["scope_id"])
        await security._authority(
            db,
            p,
            {
                "security.grants.manage",
                *REQUIRED,
                *({"exam.score.review"} if export_allowed else set()),
            },
            scope,
            delegation=True,
        )
        if not await covered(db, grant, session_id, export_allowed):
            raise AccessError(DENIED, 404)
        if op.replay is not None:
            return op.replay
        await exams.row(db, exams.S, session_id, lock=True)
        member = await db.scalar(
            select(E.c.id)
            .join(R, R.c.id == E.c.roster_revision_id)
            .where(
                R.c.exam_session_id == session_id,
                R.c.is_current,
                R.c.status == "frozen",
                E.c.administrative_class_id == class_id,
            )
            .limit(1)
        )
        if member is None:
            raise AccessError(DENIED, 404)
        identifier = uuid4()
        await db.execute(
            insert(C).values(
                id=identifier,
                grant_id=grant_id,
                exam_session_id=session_id,
                administrative_class_id=class_id,
                export_allowed=export_allowed,
                created_by_user_id=p.user_id,
            )
        )
        op.result = dict(class_access_id=str(identifier), active=True)
        return op.result


async def revoke_class(db, p, request_id, identifier):
    async with security.operation(
        db, p, request_id, "revoke", dict(class_access_id=str(identifier))
    ) as op:
        row = (
            (
                await db.execute(
                    select(C, G.c.scope_id, G.c.role_version_id)
                    .join(G, G.c.id == C.c.grant_id)
                    .where(C.c.id == identifier)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AccessError(DENIED, 404)
        scope = await security.scope(db, row["scope_id"])
        await security._authority(
            db,
            p,
            {
                "security.grants.manage",
                *REQUIRED,
                *({"exam.score.review"} if row["export_allowed"] else set()),
            },
            scope,
            delegation=True,
        )
        if op.replay is not None:
            return op.replay
        await db.execute(
            update(C)
            .where(C.c.id == identifier, C.c.revoked_at.is_(None))
            .values(revoked_at=exams.func.clock_timestamp())
        )
        op.result = dict(class_access_id=str(identifier), active=False)
        return op.result
