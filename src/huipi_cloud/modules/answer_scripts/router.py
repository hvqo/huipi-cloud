"""Fixed authenticated HTTP surface for manual grouping, never an OCR bridge."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.scans.router import ScanRoute
from huipi_cloud.modules.submissions.router import Storage

from . import service
from .schemas import (
    CandidateList,
    ScriptChange,
    ScriptCreate,
    ScriptList,
    ScriptManifest,
    ScriptRead,
    ScriptReceipt,
    SourcePageList,
    VersionList,
)

router = APIRouter(tags=["exam-answer-scripts"], route_class=ScanRoute)
Limit = Annotated[int, Query(ge=1, le=20)]
Offset = Annotated[int, Query(ge=0, le=100000)]


@router.get("/exam-sessions/{exam_session_id}/script-pages", response_model=SourcePageList)
async def list_script_pages(
    exam_session_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: Offset = 0,
    limit: Limit = 20,
    batch_id: UUID | None = None,
    unclaimed: bool = False,
):
    return await service.pages(
        session, principal, exam_session_id, offset, limit, batch_id, unclaimed
    )


@router.get("/exam-sessions/{exam_session_id}/identity-candidates", response_model=CandidateList)
async def list_script_identities(
    exam_session_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    q: Annotated[str, Query(max_length=100)] = "",
    offset: Offset = 0,
    limit: Limit = 20,
):
    return await service.candidates(session, principal, exam_session_id, q, offset, limit)


@router.post(
    "/exam-sessions/{exam_session_id}/answer-scripts", response_model=ScriptReceipt, status_code=201
)
async def create_answer_script(
    exam_session_id: UUID,
    body: ScriptCreate,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await service.write(session, principal, storage, exam_session_id, body)


@router.get("/exam-sessions/{exam_session_id}/answer-scripts", response_model=ScriptList)
async def list_answer_scripts(
    exam_session_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: Offset = 0,
    limit: Limit = 10,
):
    return await service.listing(session, principal, exam_session_id, offset, limit)


@router.get("/answer-scripts/{script_id}", response_model=ScriptRead)
async def inspect_answer_script(
    script_id: UUID, session: DbSession, principal: AuthorizedPrincipal
):
    return await service.inspect(session, principal, script_id)


@router.get("/answer-scripts/{script_id}/versions", response_model=VersionList)
async def list_answer_script_versions(
    script_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: Offset = 0,
    limit: Limit = 10,
):
    return await service.history(session, principal, script_id, offset, limit)


@router.post("/answer-scripts/{script_id}/versions", response_model=ScriptReceipt)
async def change_answer_script(
    script_id: UUID,
    body: ScriptChange,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    # Resolve only the trusted parent. Actual write authorizes again inside both UoWs.
    async with service.scans.transaction(session):
        sid = (await service.exams.row(session, service.A, script_id))["exam_session_id"]
    return await service.write(session, principal, storage, sid, body, script_id)


@router.get("/answer-scripts/{script_id}/manifest", response_model=ScriptManifest)
async def get_answer_script_manifest(
    script_id: UUID, session: DbSession, principal: AuthorizedPrincipal, storage: Storage
):
    return await service.manifest(session, principal, storage, script_id)
