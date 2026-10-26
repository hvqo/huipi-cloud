"""Thin authenticated web entrypoints; all writes use original paper transactions."""

from uuid import UUID

from fastapi import APIRouter, Query

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.exam_configuration.router import ConfigurationRoute
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal

from . import schemas as original
from . import service as papers
from . import web_schemas as s
from . import web_service as service

router = APIRouter(prefix="/papers", tags=["papers"], route_class=ConfigurationRoute)
Offset = Query(0, ge=0, le=10000)


@router.get("", response_model=s.PaperWebPage)
async def paper_web_list(db: DbSession, p: AuthorizedPrincipal, offset: int = Offset):
    return await service.listing(db, p, offset)


@router.get("/sources", response_model=s.PaperWebSources)
async def paper_web_sources(db: DbSession, p: AuthorizedPrincipal, offset: int = Offset):
    return await service.sources(db, p, offset)


@router.post("", response_model=s.PaperWebWriteResult, status_code=201)
async def paper_web_create(body: original.SnapshotCreate, db: DbSession, p: AuthorizedPrincipal):
    return await papers.create_member_snapshot(db, p, body)


@router.get("/{paper_id}", response_model=s.PaperWebDetail)
async def paper_web_read(paper_id: UUID, db: DbSession, p: AuthorizedPrincipal):
    return await service.detail(db, p, paper_id)


@router.get("/{paper_id}/history", response_model=s.PaperWebPage)
async def paper_web_history(
    paper_id: UUID, db: DbSession, p: AuthorizedPrincipal, offset: int = Offset
):
    return await service.listing(db, p, offset, family_id=paper_id)


@router.post("/{paper_id}/questions", response_model=s.PaperWebWriteResult)
async def paper_web_edit(
    paper_id: UUID, body: s.PaperWebQuestionEdit, db: DbSession, p: AuthorizedPrincipal
):
    request = original.QuestionEdit(paper_version_id=paper_id, **body.model_dump())
    return await papers.edit_question(db, p, request)


@router.post("/{paper_id}/order", response_model=s.PaperWebWriteResult)
async def paper_web_reorder(
    paper_id: UUID, body: s.PaperWebReorder, db: DbSession, p: AuthorizedPrincipal
):
    return await papers.reorder(
        db, p, original.Reorder(paper_version_id=paper_id, **body.model_dump())
    )


@router.post("/{paper_id}/lock", response_model=s.PaperWebWriteResult)
async def paper_web_lock(
    paper_id: UUID, body: s.PaperWebWrite, db: DbSession, p: AuthorizedPrincipal
):
    return await papers.lock_paper(
        db, p, original.VersionWrite(paper_version_id=paper_id, **body.model_dump())
    )


@router.post("/{paper_id}/fork", response_model=s.PaperWebWriteResult, status_code=201)
async def paper_web_fork(
    paper_id: UUID, body: s.PaperWebWrite, db: DbSession, p: AuthorizedPrincipal
):
    return await papers.fork_paper(
        db, p, original.VersionWrite(paper_version_id=paper_id, **body.model_dump())
    )


@router.post("/{paper_id}/retire", response_model=s.PaperWebWriteResult)
async def paper_web_retire(
    paper_id: UUID, body: s.PaperWebWrite, db: DbSession, p: AuthorizedPrincipal
):
    return await papers.retire_paper(
        db, p, original.VersionWrite(paper_version_id=paper_id, **body.model_dump())
    )
