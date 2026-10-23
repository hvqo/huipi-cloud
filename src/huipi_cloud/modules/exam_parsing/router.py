"""Teacher-facing intent and results only. No worker-control HTTP endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path

from huipi_cloud.core.config import settings
from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.scans.router import ScanRoute
from huipi_cloud.modules.submissions.router import Storage

from . import service
from .schemas import ParsePage, ParseReceipt, ParseRequest, ParseStatus

router = APIRouter(tags=["exam-parsing"], route_class=ScanRoute)


@router.post("/answer-scripts/{script_id}/parsing", response_model=ParseReceipt, status_code=202)
async def start_exam_parsing(
    script_id: UUID,
    body: ParseRequest,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await service.start(session, principal, storage, script_id, body)


@router.get("/answer-scripts/{script_id}/parsing", response_model=ParseStatus)
async def current_exam_parsing(script_id: UUID, session: DbSession, principal: AuthorizedPrincipal):
    return await service.status(session, principal, aid=script_id)


@router.get("/exam-parsing-jobs/{job_id}", response_model=ParseStatus)
async def inspect_exam_parsing(job_id: UUID, session: DbSession, principal: AuthorizedPrincipal):
    return await service.status(session, principal, jid=job_id)


@router.get("/exam-parsing-jobs/{job_id}/pages/{page_number}", response_model=ParsePage)
async def read_exam_parsing_page(
    job_id: UUID,
    page_number: Annotated[int, Path(ge=0, le=99)],
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await service.page(session, principal, storage, job_id, page_number, settings)
