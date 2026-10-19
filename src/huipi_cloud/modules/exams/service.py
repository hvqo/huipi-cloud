"""Short authorized transactions for exam planning, never student/answer access."""

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.modules.exams.models import ExamEvent, ExamOperation, ExamSession, SessionPaper
from huipi_cloud.modules.exams.permissions import EXAM_OPERATOR_ROLE
from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.models import User
from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.organization.models import AcademicTerm, Subject
from huipi_cloud.modules.papers import service as papers
from huipi_cloud.modules.papers.models import PaperVersion
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityGrant, SecurityScope

E, S, B, OPS = (m.__table__ for m in (ExamEvent, ExamSession, SessionPaper, ExamOperation))
P = PaperVersion.__table__
DENIED = "resource_not_found_or_forbidden"
SCHOOL_ZONE = ZoneInfo("Asia/Shanghai")
MAX_CONTENT_BYTES = 128 * 1024


def digest(value):
    return papers.digest(value)


@dataclass
class Operation:
    replay: dict | None
    result: dict | None = None


@asynccontextmanager
async def operation(session, principal, request_id, action, payload):
    fingerprint = digest({"action": action, "payload": payload})
    committing = False
    try:
        await session.begin()
        await session.execute(select(func.set_config("statement_timeout", "8000", True)))
        await identity.refresh_principal(session, principal, lock=True)
        key = int.from_bytes(
            hashlib.sha256(f"exam:{principal.user_id}:{request_id}".encode()).digest()[:8],
            signed=True,
        )
        await session.execute(select(func.pg_advisory_xact_lock(key)))
        old = (
            (
                await session.execute(
                    select(OPS).where(
                        OPS.c.actor_user_id == principal.user_id,
                        OPS.c.request_id == request_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if old and old["payload_sha256"] != fingerprint:
            raise AccessError("exam_request_conflict", 409)
        op = Operation(dict(old["result"]) if old else None)
        yield op
        if op.replay is None:
            if op.result is None:
                raise RuntimeError("exam operation missing result")
            await session.execute(
                insert(OPS).values(
                    id=uuid4(),
                    actor_user_id=principal.user_id,
                    request_id=request_id,
                    action=action,
                    payload_sha256=fingerprint,
                    result=op.result,
                )
            )
        committing = True
        await session.commit()
    except BaseException as error:
        try:
            await session.rollback()
        except (SQLAlchemyError, OSError, TimeoutError):
            pass
        if isinstance(error, (SQLAlchemyError, OSError, TimeoutError)):
            code = getattr(getattr(error, "orig", None), "sqlstate", None)
            if not committing and code in {"23505", "23514", "23503", "40001", "40P01"}:
                raise AccessError("exam_write_conflict", 409) from None
            raise AccessError(
                "exam_commit_outcome_unknown" if committing else "exam_operation_unavailable",
                503,
            ) from None
        raise


async def authorize(session, principal, kind, identifier, actions):
    column = SecurityScope.exam_event_id if kind == "exam_event" else SecurityScope.exam_session_id
    scope_id = await session.scalar(
        select(SecurityScope.id).where(SecurityScope.kind == kind, column == identifier)
    )
    if scope_id is None:
        raise AccessError(DENIED, 404)
    await security._authority(
        session, principal, set(actions), await security.scope(session, scope_id)
    )


async def row(session, table, identifier, *, lock=False, share=False):
    stmt = select(table).where(table.c.id == identifier)
    if lock or share:
        stmt = stmt.with_for_update(read=share)
    value = (await session.execute(stmt)).mappings().one_or_none()
    if value is None:
        raise AccessError(DENIED, 404)
    return value


def public(value):
    return json.loads(json.dumps(dict(value), default=str))


def revision(value, expected):
    if value["revision"] != expected:
        raise AccessError("exam_revision_conflict", 409)


async def parent_and_session(session, request):
    # Every session writer fences parent state, then locks the session, then papers.
    value = await row(session, S, request.exam_session_id)
    parent = await row(session, E, value["exam_event_id"], share=True)
    value = await row(session, S, request.exam_session_id, lock=True)
    revision(value, request.expected_revision)
    if parent["lifecycle"] not in {"draft", "active"}:
        raise AccessError("exam_event_not_editable", 409)
    return parent, value


async def bump(session, table, value, **changes):
    await session.execute(
        update(table)
        .where(table.c.id == value["id"])
        .values(revision=value["revision"] + 1, updated_at=func.clock_timestamp(), **changes)
    )
    return public(await row(session, table, value["id"]))


async def check_schedule(session, parent, subject_id, starts, ends):
    term = await session.get(
        AcademicTerm, parent["academic_term_id"], with_for_update={"read": True}
    )
    subject = await session.get(Subject, subject_id, with_for_update={"read": True})
    if subject is None or not subject.is_active:
        raise AccessError("exam_subject_unavailable", 409)
    if not (
        term.start_date
        <= starts.astimezone(SCHOOL_ZONE).date()
        <= ends.astimezone(SCHOOL_ZONE).date()
        <= term.end_date
    ):
        raise AccessError("exam_time_outside_term", 409)


async def create_event(session, principal, request):
    async with operation(
        session, principal, request.request_id, "create_event", request.model_dump(mode="json")
    ) as op:
        await papers.authorize_root(session, principal, "exam.event.create")
        if op.replay is not None:
            return op.replay
        if (
            await session.get(
                AcademicTerm, request.academic_term_id, with_for_update={"read": True}
            )
            is None
        ):
            raise AccessError(DENIED, 404)
        identifier = uuid4()
        await session.execute(
            insert(E).values(
                id=identifier,
                academic_term_id=request.academic_term_id,
                title=request.title,
                category=request.category,
                organizer_user_id=principal.user_id,
                created_by_user_id=principal.user_id,
            )
        )
        await session.execute(
            insert(SecurityScope).values(id=uuid4(), kind="exam_event", exam_event_id=identifier)
        )
        op.result = public(await row(session, E, identifier))
        return op.result


async def create_session(session, principal, request):
    async with operation(
        session, principal, request.request_id, "create_session", request.model_dump(mode="json")
    ) as op:
        await authorize(
            session, principal, "exam_event", request.exam_event_id, {"exam.event.manage"}
        )
        if op.replay is not None:
            return op.replay
        parent = await row(session, E, request.exam_event_id, lock=True)
        revision(parent, request.expected_event_revision)
        if parent["lifecycle"] != "draft":
            raise AccessError("exam_event_not_draft", 409)
        if (
            await session.scalar(
                select(func.count()).select_from(S).where(S.c.exam_event_id == parent["id"])
            )
            >= 200
        ):
            raise AccessError("exam_session_limit", 409)
        await check_schedule(
            session, parent, request.subject_id, request.starts_at, request.ends_at
        )
        identifier = uuid4()
        payload = request.model_dump(exclude={"request_id", "expected_event_revision"})
        await session.execute(
            insert(S).values(id=identifier, created_by_user_id=principal.user_id, **payload)
        )
        await session.execute(
            insert(SecurityScope).values(
                id=uuid4(), kind="exam_session", exam_session_id=identifier
            )
        )
        op.result = public(await row(session, S, identifier))
        op.result["event_revision"] = (await bump(session, E, parent))["revision"]
        return op.result


async def edit_event(session, principal, request):
    async with operation(
        session, principal, request.request_id, "edit_event", request.model_dump(mode="json")
    ) as op:
        await authorize(
            session, principal, "exam_event", request.exam_event_id, {"exam.event.manage"}
        )
        if op.replay is not None:
            return op.replay
        value = await row(session, E, request.exam_event_id, lock=True)
        revision(value, request.expected_revision)
        if value["lifecycle"] != "draft":
            raise AccessError("exam_event_not_draft", 409)
        op.result = await bump(session, E, value, title=request.title, category=request.category)
        return op.result


async def edit_session(session, principal, request):
    async with operation(
        session, principal, request.request_id, "edit_session", request.model_dump(mode="json")
    ) as op:
        await authorize(
            session, principal, "exam_session", request.exam_session_id, {"exam.session.manage"}
        )
        if op.replay is not None:
            return op.replay
        parent, value = await parent_and_session(session, request)
        if value["lifecycle"] != "draft":
            raise AccessError("exam_session_not_draft", 409)
        await check_schedule(
            session, parent, value["subject_id"], request.starts_at, request.ends_at
        )
        op.result = await bump(
            session,
            S,
            value,
            **request.model_dump(exclude={"request_id", "exam_session_id", "expected_revision"}),
        )
        return op.result


async def bind_paper(session, principal, request):
    async with operation(
        session, principal, request.request_id, "bind", request.model_dump(mode="json")
    ) as op:
        # Manage+bind must be in ONE grant covering this session. Paper use is a distinct
        # capability on a different real resource, never inferred from either read grant.
        await authorize(
            session,
            principal,
            "exam_session",
            request.exam_session_id,
            {"exam.session.manage", "exam.session.paper.bind"},
        )
        await papers.authorize_paper(session, principal, request.paper_version_id, "paper.use")
        if op.replay is not None:
            return op.replay
        _, value = await parent_and_session(session, request)
        if value["lifecycle"] != "draft":
            raise AccessError("exam_session_not_draft", 409)
        p = await row(session, P, request.paper_version_id, share=True)
        if p["status"] != "locked":
            raise AccessError("exam_paper_not_usable", 409)
        if value["paper_policy"] == "single" and request.variant_code != "A":
            raise AccessError("exam_variant_not_allowed", 409)
        if (
            await session.scalar(
                select(func.count()).select_from(B).where(B.c.exam_session_id == value["id"])
            )
            >= 200
        ):
            raise AccessError("exam_binding_history_limit", 409)
        identifier = uuid4()
        await session.execute(
            insert(B).values(
                id=identifier,
                exam_session_id=value["id"],
                paper_version_id=p["id"],
                variant_code=request.variant_code,
                content_sha256=p["content_sha256"],
                mapping_digest=p["mapping_digest"],
                created_by_user_id=principal.user_id,
            )
        )
        op.result = {
            "binding": public(await row(session, B, identifier)),
            "session": await bump(session, S, value),
        }
        return op.result


async def unbind_paper(session, principal, request):
    async with operation(
        session, principal, request.request_id, "unbind", request.model_dump(mode="json")
    ) as op:
        await authorize(
            session,
            principal,
            "exam_session",
            request.exam_session_id,
            {"exam.session.manage", "exam.session.paper.bind"},
        )
        if op.replay is not None:
            return op.replay
        _, value = await parent_and_session(session, request)
        if value["lifecycle"] != "draft":
            raise AccessError("exam_session_not_draft", 409)
        binding = await row(session, B, request.session_paper_id)
        if binding["exam_session_id"] != value["id"]:
            raise AccessError(DENIED, 404)
        if binding["status"] != "active":
            raise AccessError("exam_binding_not_active", 409)
        await session.execute(
            update(B)
            .where(B.c.id == binding["id"])
            .values(status="revoked", revoked_at=func.clock_timestamp())
        )
        op.result = {
            "binding": public(await row(session, B, binding["id"])),
            "session": await bump(session, S, value),
        }
        return op.result


async def change_event_state(session, principal, request):
    async with operation(
        session, principal, request.request_id, "event_state", request.model_dump(mode="json")
    ) as op:
        await authorize(
            session, principal, "exam_event", request.exam_event_id, {"exam.event.manage"}
        )
        if op.replay is not None:
            return op.replay
        value = await row(session, E, request.exam_event_id, lock=True)
        revision(value, request.expected_revision)
        allowed = {"draft": {"active", "cancelled"}, "active": {"closed", "cancelled"}}
        if request.lifecycle not in allowed.get(value["lifecycle"], set()):
            raise AccessError("exam_state_conflict", 409)
        states = list(
            await session.scalars(select(S.c.lifecycle).where(S.c.exam_event_id == value["id"]))
        )
        if request.lifecycle == "active" and (not states or any(s != "ready" for s in states)):
            raise AccessError("exam_sessions_not_ready", 409)
        if request.lifecycle in {"closed", "cancelled"} and any(
            s not in {"closed", "cancelled"} for s in states
        ):
            raise AccessError("exam_sessions_not_closed", 409)
        if request.lifecycle == "active":
            query = (
                select(P)
                .join(B, B.c.paper_version_id == P.c.id)
                .join(S, S.c.id == B.c.exam_session_id)
                .where(S.c.exam_event_id == value["id"], B.c.status == "active")
                .order_by(P.c.id)
                .with_for_update(read=True, of=P)
            )
            for paper in (await session.execute(query)).mappings():
                if paper["status"] != "locked":
                    raise AccessError("exam_paper_not_usable", 409)
        op.result = await bump(session, E, value, lifecycle=request.lifecycle)
        return op.result


async def change_session_state(session, principal, request):
    async with operation(
        session, principal, request.request_id, "session_state", request.model_dump(mode="json")
    ) as op:
        await authorize(
            session, principal, "exam_session", request.exam_session_id, {"exam.session.manage"}
        )
        if op.replay is not None:
            return op.replay
        parent, value = await parent_and_session(session, request)
        allowed = {"draft": {"ready", "cancelled"}, "ready": {"closed", "cancelled"}}
        if request.lifecycle not in allowed.get(value["lifecycle"], set()):
            raise AccessError("exam_state_conflict", 409)
        if request.lifecycle == "ready":
            bindings = list(
                (
                    await session.execute(
                        select(B)
                        .where(B.c.exam_session_id == value["id"], B.c.status == "active")
                        .order_by(B.c.variant_code)
                    )
                ).mappings()
            )
            expected = ["A"] if value["paper_policy"] == "single" else ["A", "B"]
            if [b["variant_code"] for b in bindings] != expected:
                raise AccessError("exam_variants_incomplete", 409)
            for binding in sorted(bindings, key=lambda b: b["paper_version_id"]):
                paper = await row(session, P, binding["paper_version_id"], share=True)
                if paper["status"] != "locked":
                    raise AccessError("exam_paper_not_usable", 409)
        # DB trigger repeats the same invariants for direct SQL writers.
        if request.lifecycle == "closed" and parent["lifecycle"] != "active":
            raise AccessError("exam_event_not_active", 409)
        op.result = await bump(session, S, value, lifecycle=request.lifecycle)
        return op.result


async def inspect_event(session, principal, identifier):
    try:
        async with session.begin():
            await authorize(session, principal, "exam_event", identifier, {"exam.event.read"})
            result = public(await row(session, E, identifier, share=True))
            result["session_count"] = await session.scalar(
                select(func.count()).select_from(S).where(S.c.exam_event_id == identifier)
            )
            result["roster_status"] = "not_configured"
            return result
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError("exam_operation_unavailable", 503) from None


async def inspect_session(session, principal, identifier):
    try:
        async with session.begin():
            await authorize(session, principal, "exam_session", identifier, {"exam.session.read"})
            result = public(await row(session, S, identifier, share=True))
            rows = (
                await session.execute(
                    select(B, P.c.status.label("paper_status"))
                    .join(P, P.c.id == B.c.paper_version_id)
                    .where(B.c.exam_session_id == identifier)
                    .order_by(B.c.created_at, B.c.id)
                    .limit(200)
                )
            ).mappings()
            result["papers"] = [public(r) for r in rows]
            result["default_variant"] = "A"
            result["roster_status"] = "not_configured"
            result["attendance_status"] = "not_available"
            return result
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError("exam_operation_unavailable", 503) from None


async def enable_operator(session, actor_user_id, target_user_id, request_id, approval_ref):
    """DB-credential deployment action, never a bearer/user-facing self-upgrade."""
    if not approval_ref.strip() or len(approval_ref) > 120:
        raise AccessError("exam_approval_required", 422)
    user = await session.get(User, actor_user_id)
    if user is None:
        raise AccessError(DENIED, 404)
    principal = Principal(
        user_id=user.id,
        issuer=user.issuer,
        subject=user.subject,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await session.rollback()
    async with operation(
        session,
        principal,
        request_id,
        "enable_operator",
        dict(target=str(target_user_id), approval_ref=approval_ref),
    ) as op:
        await papers.authorize_root(session, principal, "security.grants.manage")
        if op.replay is not None:
            return op.replay
        target = await session.scalar(
            select(User)
            .where(User.id == target_user_id, User.is_active.is_(True))
            .with_for_update(read=True)
        )
        if target is None:
            raise AccessError(DENIED, 404)
        grant = SecurityGrant(
            user_id=target_user_id,
            role_version_id=EXAM_OPERATOR_ROLE,
            scope_id=security.INSTALLATION_SCOPE,
        )
        session.add(grant)
        await session.flush()
        op.result = {
            "grant_id": str(grant.id),
            "target_user_id": str(target_user_id),
            "role_version_id": str(EXAM_OPERATOR_ROLE),
        }
        return op.result
