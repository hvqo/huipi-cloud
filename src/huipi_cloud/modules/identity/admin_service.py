"""Short admin transactions: recheck DB authority, mutate and append an immutable audit."""

import hashlib
import json
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.database.base import utc_now
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.identity import service
from huipi_cloud.modules.identity.http_schemas import (
    AdminAction,
    AdminOperationResult,
    Capability,
    CurrentUser,
    Role,
    TeacherActivation,
    TeacherRegistration,
)
from huipi_cloud.modules.identity.models import AdminOperation, AssignmentMembership, User
from huipi_cloud.modules.identity.protocol import AccessError, Principal


@asynccontextmanager
async def administrator_transaction(session: AsyncSession, principal: Principal):
    async with session.begin():
        await session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
        current = await service.refresh_principal(session, principal, lock=True)
        if not current.is_admin:
            raise AccessError("administrator_required")
        yield current


async def current_user(session: AsyncSession, principal: Principal) -> CurrentUser:
    async with session.begin():
        current = await service.refresh_principal(session, principal)
        capabilities = [
            Capability.CREATE_ASSIGNMENT,
            Capability.READ_SCOPED,
            Capability.WRITE_SCOPED,
            Capability.REVIEW_SCOPED,
        ]
        if current.is_admin:
            capabilities += [
                Capability.ACCESS_ALL,
                Capability.MANAGE_TEACHERS,
                Capability.MANAGE_MEMBERSHIPS,
                Capability.READ_AUDIT,
            ]
        return CurrentUser(
            user_id=current.user_id,
            role=Role.ADMIN if current.is_admin else Role.TEACHER,
            capabilities=capabilities,
            is_active=True,
        )


def operation_result(row: AdminOperation) -> AdminOperationResult:
    return AdminOperationResult(
        operation_id=row.id,
        request_id=row.request_id,
        action=row.action,
        target_user_id=row.target_user_id,
        assignment_id=row.assignment_id,
        state=row.state,
        created_at=row.created_at,
    )


async def replay(
    session: AsyncSession,
    principal: Principal,
    request_id: UUID,
    action: AdminAction,
    payload: dict,
) -> tuple[AdminOperation | None, str]:
    digest = hashlib.sha256(
        json.dumps(
            {"action": action, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    # Serialize only this actor/request pair. No lock is held during external I/O.
    lock_key = int.from_bytes(
        hashlib.sha256(f"{principal.user_id}:{request_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    await session.execute(select(func.pg_advisory_xact_lock(lock_key)))
    old = await session.scalar(
        select(AdminOperation).where(
            AdminOperation.actor_user_id == principal.user_id,
            AdminOperation.request_id == request_id,
        )
    )
    if old and old.payload_sha256 != digest:
        raise AccessError("admin_request_conflict", 409)
    return old, digest


async def append(
    session: AsyncSession,
    principal: Principal,
    request_id: UUID,
    digest: str,
    action: AdminAction,
    target_user_id: UUID,
    state: dict[str, bool],
    assignment_id: UUID | None = None,
) -> AdminOperationResult:
    row = AdminOperation(
        actor_user_id=principal.user_id,
        request_id=request_id,
        payload_sha256=digest,
        action=action,
        target_user_id=target_user_id,
        assignment_id=assignment_id,
        state=state,
    )
    session.add(row)
    await session.flush()
    return operation_result(row)


async def teacher(session: AsyncSession, user_id: UUID, *, lock=False) -> User:
    query = select(User).where(User.id == user_id, User.is_admin.is_(False))
    row = await session.scalar(query.with_for_update() if lock else query)
    if row is None:
        # Admin accounts have a separate deployment policy; never edit via teacher APIs.
        raise AccessError("teacher_not_found", 404)
    return row


async def register_teacher(session, principal, payload: TeacherRegistration):
    async with administrator_transaction(session, principal) as current:
        old, digest = await replay(
            session,
            current,
            payload.request_id,
            AdminAction.REGISTER_TEACHER,
            {"issuer": payload.issuer, "subject": payload.subject},
        )
        if old:
            return operation_result(old)
        if not settings.auth_issuer or payload.issuer != settings.auth_issuer:
            raise AccessError("identity_issuer_not_configured", 422)
        user_id = await session.scalar(
            insert(User)
            .values(
                issuer=payload.issuer,
                subject=payload.subject,
                is_admin=False,
                is_active=True,
            )
            .on_conflict_do_nothing(constraint="uq_users_issuer_subject")
            .returning(User.id)
        )
        if user_id is None:
            raise AccessError("identity_already_registered", 409)
        return await append(
            session,
            current,
            payload.request_id,
            digest,
            AdminAction.REGISTER_TEACHER,
            user_id,
            {"is_active": True, "is_admin": False},
        )


async def activate_teacher(session, principal, user_id: UUID, payload: TeacherActivation):
    async with administrator_transaction(session, principal) as current:
        old, digest = await replay(
            session,
            current,
            payload.request_id,
            AdminAction.ACTIVATE_TEACHER,
            {"user_id": str(user_id), "is_active": payload.is_active},
        )
        if old:
            return operation_result(old)
        row = await teacher(session, user_id, lock=True)
        row.is_active = payload.is_active
        return await append(
            session,
            current,
            payload.request_id,
            digest,
            AdminAction.ACTIVATE_TEACHER,
            user_id,
            {"is_active": payload.is_active, "is_admin": False},
        )


async def change_membership(
    session,
    principal,
    assignment_id: UUID,
    user_id: UUID,
    request_id: UUID,
    *,
    can_write: bool | None,
):
    action = AdminAction.REVOKE_ASSIGNMENT if can_write is None else AdminAction.GRANT_ASSIGNMENT
    async with administrator_transaction(session, principal) as current:
        old, digest = await replay(
            session,
            current,
            request_id,
            action,
            {
                "assignment_id": str(assignment_id),
                "user_id": str(user_id),
                "can_write": can_write,
            },
        )
        if old:
            return operation_result(old)
        if (
            await session.scalar(
                select(Assignment.id)
                .where(Assignment.id == assignment_id)
                .with_for_update(read=True)
            )
            is None
        ):
            raise AccessError("resource_not_found_or_forbidden", 404)
        target = await teacher(session, user_id, lock=True)
        if can_write is not None:
            if not target.is_active:
                raise AccessError("teacher_inactive", 409)
            await session.execute(
                insert(AssignmentMembership)
                .values(
                    assignment_id=assignment_id,
                    user_id=user_id,
                    can_write=can_write,
                )
                .on_conflict_do_update(
                    constraint="uq_assignment_memberships_user",
                    set_={
                        "can_write": can_write,
                        "revoked_at": None,
                    },
                )
            )
        else:
            await session.execute(
                update(AssignmentMembership)
                .where(
                    AssignmentMembership.assignment_id == assignment_id,
                    AssignmentMembership.user_id == user_id,
                    AssignmentMembership.revoked_at.is_(None),
                )
                .values(revoked_at=utc_now())
            )
        return await append(
            session,
            current,
            request_id,
            digest,
            action,
            user_id,
            {"is_active": can_write is not None, "can_write": bool(can_write)},
            assignment_id,
        )
