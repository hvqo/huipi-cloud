"""Authenticated result previews and protected official publication endpoints."""

from uuid import UUID

from fastapi import APIRouter, Query, Response

from huipi_cloud.core.config import settings
from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.exam_grading.router import safe
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.scans.router import ScanRoute
from huipi_cloud.modules.submissions.router import Storage

from . import final_service, service, xlsx
from .final_schemas import AttendanceInput, PrepareInput, TransitionInput
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


@router.get("/exam-sessions/{exam_session_id}/final-results/attendance", response_model=dict)
@safe
async def list_final_attendance(
    exam_session_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    class_id: UUID | None = None,
):
    return await final_service.attendance_list(session, principal, exam_session_id, class_id)


@router.post("/exam-sessions/{exam_session_id}/final-results/attendance", response_model=dict)
@safe
async def record_final_attendance(
    exam_session_id: UUID, body: AttendanceInput, session: DbSession, principal: AuthorizedPrincipal
):
    return await final_service.record_attendance(session, principal, exam_session_id, body)


@router.get("/exam-sessions/{exam_session_id}/final-results/versions", response_model=dict)
@safe
async def list_final_versions(
    exam_session_id: UUID, session: DbSession, principal: AuthorizedPrincipal
):
    return await final_service.versions(session, principal, exam_session_id)


@router.post("/exam-sessions/{exam_session_id}/final-results/versions", response_model=dict)
@safe
async def prepare_final_version(
    exam_session_id: UUID,
    body: PrepareInput,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await final_service.prepare(session, principal, storage, exam_session_id, body, settings)


@router.post(
    "/exam-sessions/{exam_session_id}/final-results/versions/{version_id}/transition",
    response_model=dict,
)
@safe
async def transition_final_version(
    exam_session_id: UUID,
    version_id: UUID,
    body: TransitionInput,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await final_service.transition(
        session, principal, storage, exam_session_id, version_id, body, settings
    )


@router.get(
    "/exam-sessions/{exam_session_id}/final-results/versions/{version_id}", response_model=dict
)
@safe
async def read_final_version(
    exam_session_id: UUID,
    version_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    class_id: UUID | None = None,
):
    value, version = await final_service.final_view(
        session, principal, exam_session_id, version_id, class_id
    )
    return {
        "version": version,
        "result": value.model_dump(mode="json"),
        "statistics": final_service.statistics(value) if value.published else None,
    }


@router.get("/exam-sessions/{exam_session_id}/final-results/versions/{version_id}/export.xlsx")
@safe
async def export_final_version(
    exam_session_id: UUID,
    version_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    class_id: UUID | None = None,
):
    value, version = await final_service.final_view(
        session, principal, exam_session_id, version_id, class_id, exporting=True
    )
    return Response(
        xlsx.export(value, formal=True, version_no=version["version_no"]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="exam-final-results.xlsx"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Result-SHA256": value.snapshot_sha256,
        },
    )
