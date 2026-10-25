"""Authenticated preview/export only. No grade mutation or publication endpoint."""

from uuid import UUID

from fastapi import APIRouter, Query, Response

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.exam_grading.router import safe
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.scans.router import ScanRoute
from huipi_cloud.modules.submissions.router import Storage

from . import service, xlsx
from .schemas import Preview, SessionsRead

router = APIRouter(tags=["exam-results-preview"], route_class=ScanRoute)


@router.get("/exam-result-sessions", response_model=SessionsRead)
@safe
async def list_exam_result_sessions(
    session: DbSession,
    principal: AuthorizedPrincipal,
    exam_event_id: UUID | None = None,
    offset: int = Query(0, ge=0, le=2000),
):
    return await service.sessions(session, principal, exam_event_id, offset)


@router.get("/exam-sessions/{exam_session_id}/results-preview", response_model=Preview)
@safe
async def read_exam_results(
    exam_session_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
    class_id: UUID | None = None,
    offset: int = Query(0, ge=0, le=2000),
    limit: int = Query(20, ge=1, le=50),
    participation_id: UUID | None = None,
):
    return await service.preview(
        session,
        principal,
        storage,
        exam_session_id,
        class_id,
        offset=offset,
        limit=limit,
        participation_id=participation_id,
    )


@router.get("/exam-sessions/{exam_session_id}/results-preview.xlsx")
@safe
async def export_exam_results(
    exam_session_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
    class_id: UUID | None = None,
):
    value = await service.preview(
        session,
        principal,
        storage,
        exam_session_id,
        class_id,
        exporting=True,
        limit=service.MAX_EXPORT_STUDENTS,
    )
    return Response(
        xlsx.export(value),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="exam-results-preview.xlsx"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Preview-SHA256": value.snapshot_sha256,
        },
    )
