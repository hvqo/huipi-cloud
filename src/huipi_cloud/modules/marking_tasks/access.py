"""Concrete task authorization; no privileged sessions, JWT roles, or caller-chosen scope."""

from sqlalchemy import select

from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.scans import service as scans

from .models import MarkingTask

T = MarkingTask.__table__


async def current(db, wid, *, lock=False):
    query = select(T).where(T.c.workspace_id == wid, T.c.is_current)
    if lock:
        query = query.with_for_update()
    return (await db.execute(query)).mappings().first()


async def authorize(db, p, sid, action):
    await exams.authorize(db, p, "exam_session", sid, {action})


async def assigned_source(db, p, wid, sid):
    """Return True only for the actual current assignee. Other callers retain old checks."""
    task = await current(db, wid)
    if task is None or task["assignee_user_id"] != p.user_id:
        return False
    if task["exam_session_id"] != sid:
        raise AccessError("resource_not_found_or_forbidden", 404)
    await authorize(db, p, sid, "exam.task.work")
    await scans.writable_session(db, sid)
    task = await current(db, wid, lock=True)
    if task["assignee_user_id"] != p.user_id or task["status"] not in {"in_progress", "completed"}:
        raise AccessError("marking_task_claim_required_or_changed", 409)
    return True


async def require_working(db, p, wid):
    task = await current(db, wid)
    if task is None:
        return None  # Existing unmanaged grading is unchanged.
    await authorize(db, p, task["exam_session_id"], "exam.task.work")
    await scans.writable_session(db, task["exam_session_id"])
    task = await current(db, wid, lock=True)
    if task["assignee_user_id"] != p.user_id or task["status"] != "in_progress":
        raise AccessError("marking_task_not_owned_or_working", 409)
    return task


async def result_valid(db, wid, review_id):
    task = await current(db, wid, lock=True)
    if task is None:
        return True
    if task["status"] != "completed" or task["review_id"] != review_id:
        return False
    from huipi_cloud.modules.exam_parsing.service import actor

    try:
        await authorize(
            db, await actor(db, task["assignee_user_id"]), task["exam_session_id"], "exam.task.work"
        )
        return True
    except AccessError:
        return False
