"""Existing service transactions behind fixed JWT and real-resource route policies."""

import asyncio
from uuid import UUID

from fastapi import APIRouter, Query

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.exam_rosters import schemas as roster_requests
from huipi_cloud.modules.exam_rosters import service as rosters
from huipi_cloud.modules.exams import schemas as exam_requests
from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.scans.router import ScanRoute

from . import schemas as s
from . import service
from .policies import PREFIX


class ConfigurationRoute(ScanRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request):
            if request.method == "POST":
                pieces, size = [], 0
                try:
                    async with asyncio.timeout(10):
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > 1024 * 1024:
                                raise AccessError("exam_configuration_body_limit", 413)
                            pieces.append(chunk)
                    request._body = b"".join(pieces)
                except TimeoutError:
                    raise AccessError("exam_configuration_body_timeout", 408) from None
            try:
                async with asyncio.timeout(120):
                    return await handler(request)
            except TimeoutError:
                raise AccessError("exam_configuration_unavailable", 503) from None

        return bounded


router = APIRouter(prefix=PREFIX, tags=["exam-configuration"], route_class=ConfigurationRoute)
Offset = Query(0, ge=0, le=10000)


@router.get("/options", response_model=s.ConfigurationOptions)
async def configuration_options(
    db: DbSession, p: AuthorizedPrincipal, offset: int = Query(0, ge=0, le=1000)
):
    return await service.options(db, p, offset)


@router.get("/events", response_model=s.ConfigurationEventPage)
async def configuration_events(db: DbSession, p: AuthorizedPrincipal, offset: int = Offset):
    return await service.events(db, p, offset)


@router.get("/events/{event_id}", response_model=s.ConfigurationEventView)
async def configuration_event(event_id: UUID, db: DbSession, p: AuthorizedPrincipal):
    return await service.event(db, p, event_id)


@router.post("/events", response_model=s.ConfigurationWriteResult, status_code=201)
async def configuration_create_event(
    body: exam_requests.EventCreate, db: DbSession, p: AuthorizedPrincipal
):
    return await exams.create_event(db, p, body)


@router.post("/events/{event_id}/state", response_model=s.ConfigurationWriteResult)
async def configuration_event_state(
    event_id: UUID, body: s.ConfigurationEventStateBody, db: DbSession, p: AuthorizedPrincipal
):
    return await exams.change_event_state(
        db, p, exam_requests.EventState(exam_event_id=event_id, **body.model_dump())
    )


@router.get("/sessions", response_model=s.ConfigurationSessionPage)
async def configuration_sessions(
    db: DbSession, p: AuthorizedPrincipal, event_id: UUID | None = None, offset: int = Offset
):
    return await service.sessions(db, p, event_id, offset)


@router.get("/sessions/{session_id}", response_model=s.ConfigurationSessionView)
async def configuration_session(session_id: UUID, db: DbSession, p: AuthorizedPrincipal):
    return await service.session(db, p, session_id)


@router.post(
    "/events/{event_id}/sessions", response_model=s.ConfigurationWriteResult, status_code=201
)
async def configuration_create_session(
    event_id: UUID, body: s.ConfigurationSessionCreateBody, db: DbSession, p: AuthorizedPrincipal
):
    return await exams.create_session(
        db, p, exam_requests.SessionCreate(exam_event_id=event_id, **body.model_dump())
    )


@router.post("/sessions/{session_id}/state", response_model=s.ConfigurationWriteResult)
async def configuration_session_state(
    session_id: UUID, body: s.ConfigurationSessionStateBody, db: DbSession, p: AuthorizedPrincipal
):
    return await exams.change_session_state(
        db, p, exam_requests.SessionState(exam_session_id=session_id, **body.model_dump())
    )


@router.get("/papers", response_model=s.ConfigurationPaperPage)
async def configuration_papers(db: DbSession, p: AuthorizedPrincipal, offset: int = Offset):
    return await service.available_papers(db, p, offset)


@router.post("/sessions/{session_id}/papers", response_model=s.ConfigurationWriteResult)
async def configuration_bind(
    session_id: UUID, body: s.ConfigurationBindBody, db: DbSession, p: AuthorizedPrincipal
):
    value = await exams.bind_paper(
        db, p, exam_requests.PaperBind(exam_session_id=session_id, **body.model_dump())
    )
    return dict(
        id=value["session"]["id"],
        revision=value["session"]["revision"],
        related_id=value["binding"]["id"],
    )


@router.post("/sessions/{session_id}/papers/revoke", response_model=s.ConfigurationWriteResult)
async def configuration_unbind(
    session_id: UUID, body: s.ConfigurationUnbindBody, db: DbSession, p: AuthorizedPrincipal
):
    value = await exams.unbind_paper(
        db, p, exam_requests.PaperUnbind(exam_session_id=session_id, **body.model_dump())
    )
    return dict(
        id=value["session"]["id"],
        revision=value["session"]["revision"],
        related_id=value["binding"]["id"],
    )


@router.post("/sessions/{session_id}/roster-preview", response_model=s.ConfigurationCandidatePage)
async def configuration_candidates(
    session_id: UUID, body: s.ConfigurationCandidateRequest, db: DbSession, p: AuthorizedPrincipal
):
    return await service.candidates(db, p, session_id, body)


@router.get("/sessions/{session_id}/rosters", response_model=s.ConfigurationRosterHistory)
async def configuration_rosters(session_id: UUID, db: DbSession, p: AuthorizedPrincipal):
    return await rosters.history(db, p, session_id)


@router.post(
    "/sessions/{session_id}/rosters", response_model=s.ConfigurationRosterSummary, status_code=201
)
async def configuration_create_roster(
    session_id: UUID, body: s.ConfigurationRosterCreateBody, db: DbSession, p: AuthorizedPrincipal
):
    return await rosters.create_roster(
        db, p, roster_requests.RosterCreate(exam_session_id=session_id, **body.model_dump())
    )


@router.get("/rosters/{roster_id}", response_model=s.ConfigurationRosterPage)
async def configuration_roster(
    roster_id: UUID,
    db: DbSession,
    p: AuthorizedPrincipal,
    offset: int = Query(0, ge=0, le=2000),
    limit: int = Query(100, ge=1, le=100),
):
    return await rosters.inspect_roster(db, p, roster_id, offset=offset, limit=limit)


@router.post("/rosters/{roster_id}/entries", response_model=s.ConfigurationRosterSummary)
async def configuration_replace_roster(
    roster_id: UUID, body: s.ConfigurationRosterReplaceBody, db: DbSession, p: AuthorizedPrincipal
):
    return await rosters.replace_entries(
        db, p, roster_requests.RosterReplace(roster_revision_id=roster_id, **body.model_dump())
    )


@router.post("/rosters/{roster_id}/freeze", response_model=s.ConfigurationRosterSummary)
async def configuration_freeze_roster(
    roster_id: UUID, body: s.ConfigurationRevisionRequest, db: DbSession, p: AuthorizedPrincipal
):
    return await rosters.freeze_roster(
        db, p, roster_requests.RosterWrite(roster_revision_id=roster_id, **body.model_dump())
    )


@router.post(
    "/rosters/{roster_id}/corrections", response_model=s.ConfigurationRosterSummary, status_code=201
)
async def configuration_correct_roster(
    roster_id: UUID,
    body: s.ConfigurationRosterCorrectionBody,
    db: DbSession,
    p: AuthorizedPrincipal,
):
    return await rosters.correct_roster(
        db, p, roster_requests.RosterCorrection(roster_revision_id=roster_id, **body.model_dump())
    )
