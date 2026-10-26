"""Account administration, deliberately separate from instructional resource access."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.identity import admin_service as service
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.identity.http_schemas import (
    AdminAuditPage,
    AdminAuditSummary,
    AdminOperationResult,
    AdminUserSummary,
    AssignmentMembershipSummary,
    CurrentUser,
    MembershipPage,
    TeacherActivation,
    TeacherPage,
    TeacherRegistration,
)
from huipi_cloud.modules.identity.models import AdminOperation, AssignmentMembership, User
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.organization.schemas import SchoolAccess


class IdentityRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                async with asyncio.timeout(5):
                    response = await handler(request)
                    response.headers["Cache-Control"] = "private, no-store"
                    return response
            except (SQLAlchemyError, OSError, TimeoutError):
                # COMMIT may have succeeded: keep request_id and replay, never assert rollback.
                raise AccessError("identity_operation_unavailable", 503) from None

        return safe_handler


router = APIRouter(
    route_class=IdentityRoute,
    tags=["identity-administration"],
    responses={
        409: {"description": "身份已登记、教师已停用或 request_id 内容冲突"},
        422: {"description": "输入不合法或 Issuer 不匹配服务配置"},
    },
)
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100000)]


def page(rows, limit, offset, schema):
    return dict(
        records=[schema(row) for row in rows[:limit]],
        limit=limit,
        offset=offset,
        has_next=len(rows) > limit,
        next_offset=offset + limit if len(rows) > limit else None,
    )


@router.get("/me", response_model=CurrentUser)
async def get_current_user(session: DbSession, principal: AuthorizedPrincipal):
    return await service.current_user(session, principal)


@router.get("/admin/teachers", response_model=TeacherPage)
async def list_teachers(
    session: DbSession,
    principal: AuthorizedPrincipal,
    limit: Limit = 25,
    offset: Offset = 0,
):
    async with service.administrator_transaction(session, principal):
        rows = list(
            await session.scalars(
                select(User)
                .where(User.is_admin.is_(False))
                .order_by(User.created_at, User.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        return page(
            rows,
            limit,
            offset,
            lambda r: AdminUserSummary(
                user_id=r.id,
                is_active=r.is_active,
                created_at=r.created_at,
            ),
        )


@router.post("/admin/teachers", response_model=AdminOperationResult, status_code=201)
async def register_teacher(
    payload: TeacherRegistration,
    session: DbSession,
    principal: AuthorizedPrincipal,
):
    return await service.register_teacher(session, principal, payload)


@router.patch("/admin/teachers/{user_id}", response_model=AdminOperationResult)
async def set_teacher_active(
    user_id: UUID,
    payload: TeacherActivation,
    session: DbSession,
    principal: AuthorizedPrincipal,
):
    return await service.activate_teacher(session, principal, user_id, payload)


@router.get("/admin/teachers/{user_id}/memberships", response_model=MembershipPage)
async def list_teacher_memberships(
    user_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    limit: Limit = 25,
    offset: Offset = 0,
):
    async with service.administrator_transaction(session, principal):
        target = await service.teacher(session, user_id)
        rows = list(
            await session.scalars(
                select(AssignmentMembership)
                .where(
                    AssignmentMembership.user_id == user_id,
                )
                .order_by(AssignmentMembership.created_at, AssignmentMembership.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        return page(
            rows,
            limit,
            offset,
            lambda r: AssignmentMembershipSummary(
                assignment_id=r.assignment_id,
                user_id=r.user_id,
                can_write=r.can_write,
                is_active=target.is_active and r.revoked_at is None,
                created_at=r.created_at,
                revoked_at=r.revoked_at,
            ),
        )


@router.get("/admin/audit-events", response_model=AdminAuditPage)
async def list_admin_audit_events(
    session: DbSession,
    principal: AuthorizedPrincipal,
    limit: Limit = 25,
    offset: Offset = 0,
    target_user_id: UUID | None = None,
    assignment_id: UUID | None = None,
    request_id: UUID | None = None,
):
    async with service.administrator_transaction(session, principal):
        query = select(AdminOperation)
        for column, value in (
            (AdminOperation.target_user_id, target_user_id),
            (AdminOperation.assignment_id, assignment_id),
            (AdminOperation.request_id, request_id),
        ):
            if value is not None:
                query = query.where(column == value)
        rows = list(
            await session.scalars(
                query.order_by(
                    AdminOperation.created_at.desc(),
                    AdminOperation.id.desc(),
                )
                .offset(offset)
                .limit(limit + 1)
            )
        )
        return page(
            rows,
            limit,
            offset,
            lambda r: AdminAuditSummary(
                **service.operation_result(r).model_dump(),
                actor_user_id=r.actor_user_id,
            ),
        )


@router.get("/me/school-access", response_model=SchoolAccess)
async def get_school_access(session: DbSession, principal: AuthorizedPrincipal):
    from huipi_cloud.modules.organization.workbench import school_access

    return await school_access(session, principal)
