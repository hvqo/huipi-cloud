"""Class-scoped authorization for new homework submissions."""

from uuid import UUID

from sqlalchemy import select

from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.protocol import AccessError

from .models import HomeworkAttempt, HomeworkOffering, HomeworkParticipant, TeachingAssignment

T, OFFER, P, A = (
    m.__table__
    for m in (TeachingAssignment, HomeworkOffering, HomeworkParticipant, HomeworkAttempt)
)
DENIED = "resource_not_found_or_forbidden"


async def teaching_classes(db, principal, term_id, subject_id, *, lock=False):
    await identity.refresh_principal(db, principal, lock=lock)
    q = (
        select(T.c.class_id)
        .where(
            T.c.teacher_id == principal.user_id,
            T.c.term_id == term_id,
            T.c.subject_id == subject_id,
            T.c.revoked_at.is_(None),
        )
        .order_by(T.c.class_id)
    )
    if lock:
        q = q.with_for_update(read=True)
    return set((await db.scalars(q)).all())


async def offering_access(db, principal, assignment_id: UUID, *, write=False, lock=False):
    await identity.require_assignment(db, principal, assignment_id, write=write, lock=lock)
    q = select(OFFER).where(OFFER.c.assignment_id == assignment_id)
    if lock:
        q = q.with_for_update(read=True)
    offering = (await db.execute(q)).mappings().first()
    if offering is None:
        raise AccessError(DENIED, 404)
    classes = await teaching_classes(
        db, principal, offering["term_id"], offering["subject_id"], lock=lock
    )
    if not classes:
        raise AccessError(DENIED, 404)
    return offering, classes


async def participant_access(db, principal, participant_id: UUID, *, write=False, lock=False):
    q = (
        select(P, OFFER.c.assignment_id, OFFER.c.term_id, OFFER.c.subject_id, OFFER.c.due_at)
        .join(OFFER, OFFER.c.id == P.c.offering_id)
        .where(P.c.id == participant_id)
    )
    if lock:
        q = q.with_for_update(of=P)
    row = (await db.execute(q)).mappings().first()
    if row is None:
        raise AccessError(DENIED, 404)
    offering, classes = await offering_access(
        db, principal, row["assignment_id"], write=write, lock=lock
    )
    if row["class_id"] not in classes:
        raise AccessError(DENIED, 404)
    return row, offering


async def submission_access(db, principal, submission_id: UUID, *, write=False, lock=False):
    """Legacy submissions are unaffected; linked attempts always require class authority."""
    attempt = (
        (await db.execute(select(A).where(A.c.submission_id == submission_id))).mappings().first()
    )
    if attempt is None:
        return None
    await participant_access(db, principal, attempt["participant_id"], write=write, lock=lock)
    if lock:
        attempt = (
            (
                await db.execute(
                    select(A).where(A.c.submission_id == submission_id).with_for_update(read=True)
                )
            )
            .mappings()
            .first()
        )
        if attempt is None:
            raise AccessError(DENIED, 404)
    if write and not attempt["is_current"]:
        raise AccessError("homework_attempt_superseded", 409)
    return attempt
