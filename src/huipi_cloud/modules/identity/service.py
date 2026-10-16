"""Database authority: JWT identifies a subject; only local rows authorize resources."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.infrastructure.auth.jwt import VerifiedIdentity
from huipi_cloud.modules.assignments.models import Assignment, Question
from huipi_cloud.modules.identity.models import AssignmentMembership, User
from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.submissions.models import Submission


def membership_filter(user_id: UUID, *, write: bool = False):
    predicates = [
        AssignmentMembership.user_id == user_id,
        AssignmentMembership.revoked_at.is_(None),
    ]
    if write:
        predicates.append(AssignmentMembership.can_write.is_(True))
    return predicates


async def resolve_principal(
    session: AsyncSession,
    identity: VerifiedIdentity,
    *,
    lock: bool = False,
) -> Principal:
    query = select(User).where(
        User.issuer == identity.issuer,
        User.subject == identity.subject,
        User.is_active.is_(True),
    )
    user = await session.scalar(query.with_for_update(read=True) if lock else query)
    if user is None:
        raise AccessError("teacher_not_provisioned")
    return Principal(
        user_id=user.id,
        issuer=user.issuer,
        subject=user.subject,
        expires_at=identity.expires_at,
        is_admin=user.is_admin,
    )


async def refresh_principal(
    session: AsyncSession,
    principal: Principal,
    *,
    lock: bool = False,
) -> Principal:
    if principal.expires_at <= datetime.now(UTC):
        raise AccessError("invalid_token", 401)
    current = await resolve_principal(
        session,
        VerifiedIdentity(
            principal.issuer,
            principal.subject,
            principal.expires_at,
        ),
        lock=lock,
    )
    if principal.expires_at <= datetime.now(UTC):
        raise AccessError("invalid_token", 401)
    if current.user_id != principal.user_id:
        raise AccessError("teacher_not_provisioned")
    return current


async def require_assignment(
    session: AsyncSession,
    principal: Principal,
    assignment_id: UUID,
    *,
    write: bool = False,
    lock: bool = False,
) -> None:
    """Called once per operation; no permission cache or JWT-role shortcut."""
    current = await refresh_principal(session, principal, lock=lock)
    query = select(Assignment.id).where(Assignment.id == assignment_id)
    if not current.is_admin and lock:
        membership = await session.scalar(
            select(AssignmentMembership)
            .where(
                AssignmentMembership.assignment_id == assignment_id,
                *membership_filter(current.user_id, write=write),
            )
            .with_for_update(read=True)
        )
        if membership is None:
            raise AccessError("resource_not_found_or_forbidden", 404)
    if not current.is_admin:
        query = query.where(
            select(AssignmentMembership.id)
            .where(
                AssignmentMembership.assignment_id == Assignment.id,
                *membership_filter(current.user_id, write=write),
            )
            .exists()
        )
    if await session.scalar(query) is None:
        # Deliberately indistinguishable from a missing resource.
        raise AccessError("resource_not_found_or_forbidden", 404)


async def resource_assignment(session: AsyncSession, kind: str, resource_id: UUID) -> UUID:
    model = {"question_id": Question, "submission_id": Submission}[kind]
    result = await session.scalar(select(model.assignment_id).where(model.id == resource_id))
    if result is None:
        raise AccessError("resource_not_found_or_forbidden", 404)
    return result
