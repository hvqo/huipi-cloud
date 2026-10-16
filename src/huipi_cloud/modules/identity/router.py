"""Compatibility membership endpoints; all writes use the audited administrator service."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.identity import admin_service
from huipi_cloud.modules.identity.admin_router import IdentityRoute
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.identity.http_schemas import AdminOperationResult

router = APIRouter(
    prefix="/assignments",
    tags=["assignment-authorization"],
    route_class=IdentityRoute,
)


class MembershipGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    can_write: StrictBool = False
    request_id: UUID | None = None


class MembershipWriteResult(BaseModel):
    assignment_id: UUID
    user_id: UUID
    can_write: bool
    operation: AdminOperationResult


@router.put("/{assignment_id}/members/{user_id}", response_model=MembershipWriteResult)
async def grant_assignment_membership(
    assignment_id: UUID,
    user_id: UUID,
    payload: MembershipGrant,
    session: DbSession,
    principal: AuthorizedPrincipal,
) -> MembershipWriteResult:
    operation = await admin_service.change_membership(
        session,
        principal,
        assignment_id,
        user_id,
        payload.request_id or uuid4(),
        can_write=payload.can_write,
    )
    return MembershipWriteResult(
        assignment_id=assignment_id,
        user_id=user_id,
        can_write=payload.can_write,
        operation=operation,
    )


@router.delete("/{assignment_id}/members/{user_id}", status_code=204)
async def revoke_assignment_membership(
    assignment_id: UUID,
    user_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    response: Response,
    request_id: UUID | None = None,
) -> None:
    operation = await admin_service.change_membership(
        session,
        principal,
        assignment_id,
        user_id,
        request_id or uuid4(),
        can_write=None,
    )
    response.headers["X-Operation-ID"] = str(operation.operation_id)
    response.headers["X-Request-ID"] = str(operation.request_id)
