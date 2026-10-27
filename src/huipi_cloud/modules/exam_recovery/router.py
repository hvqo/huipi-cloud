from uuid import UUID

from fastapi import APIRouter, Query

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.scans.router import ScanRoute

from . import service
from .schemas import Kind, RecoveryView

router = APIRouter(tags=["exam-recovery"], route_class=ScanRoute)


@router.get("/recovery-sessions", response_model=dict)
async def list_recovery_sessions(
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: int = Query(default=0, ge=0, le=100000),
):
    return await service.sessions(session, principal, offset)


@router.get("/exam-sessions/{session_id}/exceptions", response_model=RecoveryView)
async def inspect_exam_recovery(
    session_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: int = Query(default=0, ge=0, le=100000),
    kind: Kind | None = None,
    summary: bool = False,
):
    return await service.inspect(
        session, principal, session_id, offset, kind, limit=0 if summary else 25
    )
