"""DB-admin only grants. No public signup, role promotion or password endpoints."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from huipi_cloud.infrastructure.database.base import utc_now
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.identity.models import AssignmentMembership, User
from huipi_cloud.modules.identity.protocol import AccessError

router = APIRouter(prefix="/assignments", tags=["assignment-authorization"])


class MembershipGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    can_write: bool = False


@router.put("/{assignment_id}/members/{user_id}")
async def grant_assignment_membership(
    assignment_id: UUID,
    user_id: UUID,
    payload: MembershipGrant,
    session: DbSession,
    principal: AuthorizedPrincipal,
) -> dict:
    async with session.begin():
        if await session.get(Assignment, assignment_id) is None:
            raise AccessError("resource_not_found_or_forbidden", 404)
        if (
            await session.scalar(
                select(User.id).where(
                    User.id == user_id,
                    User.is_active.is_(True),
                )
            )
            is None
        ):
            raise AccessError("teacher_not_found", 404)
        await session.execute(
            insert(AssignmentMembership)
            .values(
                assignment_id=assignment_id,
                user_id=user_id,
                can_write=payload.can_write,
            )
            .on_conflict_do_update(
                constraint="uq_assignment_memberships_user",
                set_={"can_write": payload.can_write, "revoked_at": None},
            )
        )
    return {"assignment_id": assignment_id, "user_id": user_id, "can_write": payload.can_write}


@router.delete("/{assignment_id}/members/{user_id}", status_code=204)
async def revoke_assignment_membership(
    assignment_id: UUID,
    user_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
) -> None:
    async with session.begin():
        await session.execute(
            update(AssignmentMembership)
            .where(
                AssignmentMembership.assignment_id == assignment_id,
                AssignmentMembership.user_id == user_id,
                AssignmentMembership.revoked_at.is_(None),
            )
            .values(revoked_at=utc_now())
        )
