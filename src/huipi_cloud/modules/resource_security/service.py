"""Trusted service/CLI security operations. No public provisioning HTTP endpoint."""

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from huipi_cloud.modules.answer_scripts.permissions import SCRIPT_ACTIONS
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.assignments.schemas import AssignmentCreate
from huipi_cloud.modules.exam_grading.permissions import MARK_ACTIONS
from huipi_cloud.modules.exam_parsing.permissions import PARSE_ACTIONS
from huipi_cloud.modules.exam_results.permissions import RESULT_ACTIONS
from huipi_cloud.modules.exam_rosters.permissions import ROSTER_ACTIONS
from huipi_cloud.modules.exams.permissions import EXAM_ACTIONS
from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.models import User
from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.marking_tasks.permissions import TASK_ACTIONS
from huipi_cloud.modules.papers.permissions import (
    PAPER_ACTIONS,
    PAPER_AUTHOR_ROLE,
    PAPER_CREATOR_ROLE,
)
from huipi_cloud.modules.resource_security.models import (
    PermissionDefinition,
    SecurityAudit,
    SecurityGrant,
    SecurityInstallation,
    SecurityRolePermission,
    SecurityRoleVersion,
    SecurityScope,
)
from huipi_cloud.modules.scans.permissions import SCAN_ACTIONS

INSTALLATION_SCOPE = UUID("11111111-b11a-4000-8000-000000000001")
OPERATOR_ROLE = UUID("11111111-b11a-4000-8000-000000000002")
CREATOR_ROLE = UUID("11111111-b11a-4000-8000-000000000003")
AUDITOR_ROLE = UUID("11111111-b11a-4000-8000-000000000004")
# B-1.1b: deployment security operator that can also delegate the installation-scoped
# organization actions. Bootstrap grants this single role so that the one-grant delegation
# rule can cover both management and organization actions; narrower staff roles stay separate.
ORGANIZATION_OPERATOR_ROLE = UUID("11111111-b11b-4000-8000-000000000001")
ORGANIZATION_MANAGER_ROLE = UUID("11111111-b11b-4000-8000-000000000002")
STUDENT_IMPORTER_ROLE = UUID("11111111-b11b-4000-8000-000000000003")
# Both code AND current persisted catalog must allow execution. Future IDs cannot be activated
# by merely inserting a row/setting enabled in the database.
CATALOG = {
    **TASK_ACTIONS,
    **RESULT_ACTIONS,
    **PAPER_ACTIONS,
    **EXAM_ACTIONS,
    **ROSTER_ACTIONS,
    **SCAN_ACTIONS,
    **SCRIPT_ACTIONS,
    **PARSE_ACTIONS,
    **MARK_ACTIONS,
    "security.grants.manage": frozenset({"installation"}),
    "security.protected.create": frozenset({"installation"}),
    "security.classification.inspect": frozenset({"installation", "assignment"}),
    "organization.read": frozenset({"installation"}),
    "organization.manage": frozenset({"installation"}),
    "student.import.preview": frozenset({"installation"}),
    "student.import.confirm": frozenset({"installation"}),
}
DENIED = "resource_not_found_or_forbidden"
GRANT_ALREADY_ACTIVE = "security_grant_already_active"


async def installation_scope(session):
    # B-1.1b migration pre-seeds the installation scope, so ordinary read authorization
    # is a pure SELECT instead of an INSERT ... ON CONFLICT in the request transaction.
    existing = await session.scalar(
        select(SecurityScope).where(SecurityScope.id == INSTALLATION_SCOPE)
    )
    if existing is None:
        await session.execute(
            insert(SecurityScope)
            .values(
                id=INSTALLATION_SCOPE,
                kind="installation",
                installation_id=1,
            )
            .on_conflict_do_nothing()
        )
    return await scope(session, INSTALLATION_SCOPE)


async def scope(session, scope_id):
    row = await session.scalar(select(SecurityScope).where(SecurityScope.id == scope_id))
    if row is None or row.kind not in {
        "installation",
        "assignment",
        "paper_version",
        "exam_event",
        "exam_session",
    }:
        raise AccessError("security_scope_invalid", 422)
    if row.kind not in {"exam_event", "exam_session"} and (
        row.exam_event_id is not None or row.exam_session_id is not None
    ):
        raise AccessError("security_scope_invalid", 422)
    if row.kind in {"exam_event", "exam_session"}:
        from huipi_cloud.modules.exams.models import ExamEvent, ExamSession

        model = ExamEvent if row.kind == "exam_event" else ExamSession
        identifier = row.exam_event_id if row.kind == "exam_event" else row.exam_session_id
        if await session.get(model, identifier) is None:
            raise AccessError("security_scope_invalid", 422)
    elif row.kind == "installation":
        if row.installation_id != 1 or row.assignment_id is not None:
            raise AccessError("security_scope_invalid", 422)
        if await session.get(SecurityInstallation, 1) is None:
            raise AccessError("security_scope_invalid", 422)
    elif row.kind == "paper_version":
        from huipi_cloud.modules.papers.models import PaperVersion

        p, a = PaperVersion.__table__, Assignment.__table__
        valid = await session.scalar(
            select(p.c.id)
            .join(a, a.c.id == p.c.canonical_assignment_id)
            .where(p.c.id == row.paper_version_id, a.c.security_classification == "exam_protected")
        )
        if valid is None or row.installation_id is not None or row.assignment_id is not None:
            raise AccessError("security_scope_invalid", 422)
    else:
        # Fixed Core query deliberately sees the real FK parent, including protected data.
        table = Assignment.__table__
        if (
            row.installation_id is not None
            or await session.scalar(select(table.c.id).where(table.c.id == row.assignment_id))
            is None
        ):
            raise AccessError("security_scope_invalid", 422)
    return row


async def role_codes(session, role_id, kind):
    if (role_id == PAPER_AUTHOR_ROLE and kind != "paper_version") or (
        role_id == PAPER_CREATOR_ROLE and kind != "installation"
    ):
        raise AccessError("security_role_scope_invalid", 422)
    role = await session.get(SecurityRoleVersion, role_id)
    if role is None or role.sealed_at is None:
        raise AccessError("security_role_invalid", 422)
    codes = set(
        (
            await session.scalars(
                select(SecurityRolePermission.permission_code).where(
                    SecurityRolePermission.role_version_id == role_id,
                )
            )
        ).all()
    )
    if not codes or len(codes) > 20:
        raise AccessError("security_permission_disabled", 403)
    for code in codes:
        definition = await session.get(PermissionDefinition, code, populate_existing=True)
        if (
            code not in CATALOG
            or definition is None
            or not definition.enabled
            or set(definition.scope_kinds) != CATALOG[code]
            or kind not in CATALOG[code]
        ):
            raise AccessError("security_permission_disabled", 403)
    return codes


def covers(parent, child):
    return parent.kind == "installation" or parent.id == child.id


async def _authority(session, principal, actions, target, *, delegation=False):
    current = await identity.refresh_principal(session, principal, lock=True)
    for action in actions:
        if action not in CATALOG or (
            not (delegation and action == "security.grants.manage")
            and target.kind not in CATALOG[action]
        ):
            raise AccessError("security_permission_disabled")
    grants = list(
        (
            await session.scalars(
                select(SecurityGrant)
                .where(
                    SecurityGrant.user_id == current.user_id,
                    SecurityGrant.revoked_at.is_(None),
                )
                .order_by(SecurityGrant.id)
                .limit(101)
                .with_for_update(read=True)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if len(grants) > 100:
        raise AccessError("security_grant_limit")
    for grant in grants:
        granted_scope = await scope(session, grant.scope_id)
        codes = await role_codes(session, grant.role_version_id, granted_scope.kind)
        # A SINGLE grant must cover all required actions AND the whole target scope.
        covered = covers(granted_scope, target)
        if granted_scope.kind == "exam_event" and target.kind == "exam_session":
            from huipi_cloud.modules.exams.models import ExamSession

            covered = (
                await session.scalar(
                    select(ExamSession.id).where(
                        ExamSession.id == target.exam_session_id,
                        ExamSession.exam_event_id == granted_scope.exam_event_id,
                    )
                )
                is not None
            )
        if actions <= codes and covered:
            return current
    raise AccessError(DENIED, 404)


async def require_permission(session, principal, action, scope_id):
    try:
        return await _authority(session, principal, {action}, await scope(session, scope_id))
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError("authorization_unavailable", 503) from None


@dataclass
class Operation:
    replay: dict | None
    result: dict | None = None


@asynccontextmanager
async def operation(session, principal, request_id, action, payload):
    digest = hashlib.sha256(
        json.dumps(
            {"action": action, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    commit_started = False
    try:
        await session.begin()
        await session.execute(select(func.set_config("statement_timeout", "5000", True)))
        await identity.refresh_principal(session, principal, lock=True)
        key = int.from_bytes(
            hashlib.sha256(
                f"security:{principal.user_id}:{request_id}".encode(),
            ).digest()[:8],
            signed=True,
        )
        await session.execute(select(func.pg_advisory_xact_lock(key)))
        previous = await session.scalar(
            select(SecurityAudit).where(
                SecurityAudit.actor_user_id == principal.user_id,
                SecurityAudit.request_id == request_id,
            )
        )
        if previous is not None and previous.payload_sha256 != digest:
            raise AccessError("security_request_conflict", 409)
        op = Operation(previous.result if previous else None)
        yield op
        if op.replay is None:
            if op.result is None:
                raise RuntimeError("security operation missing result")
            session.add(
                SecurityAudit(
                    actor_user_id=principal.user_id,
                    request_id=request_id,
                    action=action,
                    payload_sha256=digest,
                    result=op.result,
                )
            )
            await session.flush()
        commit_started = True
        await session.commit()
    except BaseException as error:
        try:
            await session.rollback()
        except (SQLAlchemyError, OSError, TimeoutError):
            pass
        if isinstance(error, (SQLAlchemyError, OSError, TimeoutError)):
            raise AccessError(
                "security_commit_outcome_unknown"
                if commit_started
                else "authorization_unavailable",
                503,
            ) from None
        raise


async def create_protected_assignment(session, principal, request_id, payload: AssignmentCreate):
    payload = AssignmentCreate.model_validate(payload.model_dump())
    async with operation(
        session, principal, request_id, "protected_create", payload.model_dump(mode="json")
    ) as op:
        root = await installation_scope(session)
        await _authority(session, principal, {"security.protected.create"}, root)
        if op.replay is not None:
            return op.replay
        row = Assignment(**payload.model_dump(), security_classification="exam_protected")
        session.add(row)
        await session.flush()
        op.result = {"assignment_id": str(row.id), "classification": "exam_protected"}
        return op.result


async def grant_role(session, principal, request_id, *, user_id, role_version_id, scope_id):
    if role_version_id == PAPER_AUTHOR_ROLE:
        raise AccessError("paper_author_role_creation_only", 403)
    payload = dict(
        user_id=str(user_id), role_version_id=str(role_version_id), scope_id=str(scope_id)
    )
    async with operation(session, principal, request_id, "grant", payload) as op:
        target_scope = await scope(session, scope_id)
        codes = await role_codes(session, role_version_id, target_scope.kind)
        await _authority(
            session, principal, {"security.grants.manage", *codes}, target_scope, delegation=True
        )
        if op.replay is not None:
            return op.replay
        if user_id == principal.user_id:
            raise AccessError("security_self_grant_forbidden")
        target = await session.scalar(
            select(User)
            .where(
                User.id == user_id,
                User.is_active.is_(True),
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if target is None:
            raise AccessError(DENIED, 404)
        row = SecurityGrant(user_id=user_id, role_version_id=role_version_id, scope_id=scope_id)
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            # Active-Grant partial unique index: a retry with a NEW request_id does not
            # silently create a second grant that a single revoke could not revoke.
            raise AccessError(GRANT_ALREADY_ACTIVE, 409) from None
        op.result = {"grant_id": str(row.id), "user_id": str(user_id), "active": True}
        return op.result


async def revoke_grant(session, principal, request_id, *, grant_id):
    async with operation(
        session, principal, request_id, "revoke", {"grant_id": str(grant_id)}
    ) as op:
        row = await session.scalar(select(SecurityGrant).where(SecurityGrant.id == grant_id))
        if row is None:
            raise AccessError(DENIED, 404)
        target_scope = await scope(session, row.scope_id)
        codes = await role_codes(session, row.role_version_id, target_scope.kind)
        await _authority(
            session, principal, {"security.grants.manage", *codes}, target_scope, delegation=True
        )
        if op.replay is not None:
            return op.replay
        # UPDATE waits for in-flight authorized operations holding a SHARE lock.
        row = await session.scalar(
            select(SecurityGrant)
            .where(SecurityGrant.id == grant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
        op.result = {"grant_id": str(row.id), "user_id": str(row.user_id), "active": False}
        return op.result


async def inspect_classification(session, principal, assignment_id, scope_id):
    try:
        async with session.begin():
            target = await scope(session, scope_id)
            if target.kind == "assignment" and target.assignment_id != assignment_id:
                raise AccessError(DENIED, 404)
            await _authority(session, principal, {"security.classification.inspect"}, target)
            table = Assignment.__table__
            value = await session.scalar(
                select(table.c.security_classification).where(
                    table.c.id == assignment_id,
                )
            )
            if value not in {"legacy_homework", "exam_protected", "unclassified"}:
                raise AccessError(DENIED, 404)
            return {"assignment_id": str(assignment_id), "classification": value}
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError("authorization_unavailable", 503) from None


async def deployment_bootstrap(session, user_id, request_id, approval_ref):
    """Database-credential operator only; explicit approval, never a public identity route."""
    if not approval_ref.strip() or len(approval_ref) > 120:
        raise AccessError("security_bootstrap_approval_required", 422)
    row = await session.get(User, user_id)
    if row is None or not row.is_active or not row.is_admin:
        await session.rollback()
        raise AccessError("security_bootstrap_user_invalid")
    principal = Principal(
        user_id=row.id,
        issuer=row.issuer,
        subject=row.subject,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await session.rollback()
    async with operation(
        session, principal, request_id, "bootstrap", {"approval_ref": approval_ref}
    ) as op:
        current = await identity.refresh_principal(session, principal, lock=True)
        if not current.is_admin:
            raise AccessError("security_bootstrap_user_invalid")
        await session.scalar(
            select(SecurityInstallation)
            .where(
                SecurityInstallation.id == 1,
            )
            .with_for_update()
        )
        if op.replay is not None:
            return op.replay
        if await session.scalar(
            select(SecurityAudit.id).where(SecurityAudit.action == "bootstrap")
        ):
            raise AccessError("security_bootstrap_already_initialized", 409)
        root = await installation_scope(session)
        row = SecurityGrant(
            user_id=user_id, role_version_id=ORGANIZATION_OPERATOR_ROLE, scope_id=root.id
        )
        session.add(row)
        await session.flush()
        op.result = {"grant_id": str(row.id), "user_id": str(user_id), "active": True}
        return op.result
