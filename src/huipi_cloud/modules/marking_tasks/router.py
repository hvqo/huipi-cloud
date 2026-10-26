import asyncio
import tempfile
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Query, Response

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.exam_grading.router import safe
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.scans.router import ScanRoute, verified_download
from huipi_cloud.modules.submissions.router import Storage

from . import service as s
from .schemas import Assignment, Intent, Transition


class TaskRoute(ScanRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request):
            if request.method == "POST":
                data, size = [], 0
                async with asyncio.timeout(10):
                    async for part in request.stream():
                        size += len(part)
                        if size > 65536:
                            raise AccessError("marking_payload_too_large", 413)
                        data.append(part)
                request._body = b"".join(data)
            return await handler(request)

        return safe(bounded)


router = APIRouter(tags=["marking-tasks"], route_class=TaskRoute)


@router.get("/marking-sessions", response_model=dict)
async def list_marking_sessions(
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: int = Query(default=0, ge=0, le=100000),
):
    return await s.sessions(session, principal, offset)


@router.get("/my-marking-tasks", response_model=dict)
async def list_my_marking_tasks(
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: int = Query(default=0, ge=0, le=100000),
):
    return await s.my_tasks(session, principal, offset)


@router.get("/exam-sessions/{session_id}/marking-pool", response_model=dict)
async def list_marking_pool(
    session_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: int = Query(default=0, ge=0, le=100000),
):
    return await s.pool(session, principal, session_id, offset)


@router.get("/exam-sessions/{session_id}/marking-teachers", response_model=dict)
async def list_marking_teachers(
    session_id: UUID, session: DbSession, principal: AuthorizedPrincipal
):
    return await s.teachers(session, principal, session_id)


@router.get("/exam-sessions/{session_id}/marking-progress", response_model=dict)
async def inspect_marking_progress(
    session_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: int = Query(default=0, ge=0, le=100000),
):
    return await s.progress(session, principal, session_id, offset)


@router.post("/exam-sessions/{session_id}/marking-jobs/{job_id}/prepare", response_model=dict)
async def prepare_marking_questions(
    session_id: UUID,
    job_id: UUID,
    body: Intent,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await s.prepare(session, principal, storage, session_id, job_id, body)


@router.post("/exam-sessions/{session_id}/marking-assignments", response_model=dict)
async def assign_marking_tasks(
    session_id: UUID, body: Assignment, session: DbSession, principal: AuthorizedPrincipal
):
    return await s.assign(session, principal, session_id, body)


@router.get("/marking-tasks/{task_id}", response_model=dict)
async def inspect_marking_task(task_id: UUID, session: DbSession, principal: AuthorizedPrincipal):
    return await s.inspect(session, principal, task_id)


@router.post("/marking-tasks/{task_id}/claim", response_model=dict)
async def claim_marking_task(
    task_id: UUID, body: Transition, session: DbSession, principal: AuthorizedPrincipal
):
    return await s.claim(session, principal, task_id, body)


@router.post("/marking-tasks/{task_id}/complete", response_model=dict)
async def complete_marking_task(
    task_id: UUID,
    body: Transition,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await s.complete(session, principal, storage, task_id, body)


@router.post("/marking-tasks/{task_id}/cancel", response_model=dict)
async def cancel_marking_task(
    task_id: UUID, body: Transition, session: DbSession, principal: AuthorizedPrincipal
):
    return await s.cancel(session, principal, task_id, body)


@router.get(
    "/marking-tasks/{task_id}/pages/{page_id}/preview",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
)
async def preview_marking_page(
    task_id: UUID,
    page_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    value = await s.preview_metadata(session, principal, task_id, page_id)
    with tempfile.TemporaryDirectory(prefix="huipi-marking-preview-") as root:
        path = await verified_download(storage, value, 2 * 1024**2, Path(root))
        content = path.read_bytes()
    # Recheck task, current identity and grants after external I/O.
    await s.preview_metadata(session, principal, task_id, page_id)
    return Response(content, media_type="image/jpeg")
