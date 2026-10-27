"""Authenticated teacher operations; no student identity is guessed by OMR."""

from uuid import UUID

from fastapi import APIRouter, Response

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.scans.router import ScanRoute
from huipi_cloud.modules.submissions.router import Storage

from . import schemas as s
from . import service

router = APIRouter(tags=["exam-omr"], route_class=ScanRoute)


@router.get("/omr/papers/{paper_id}", response_model=s.TemplateRead)
async def omr_paper_view(paper_id: UUID, db: DbSession, p: AuthorizedPrincipal):
    return await service.template_view(db, p, paper_id)


@router.post("/omr/papers/{paper_id}", response_model=s.WriteReceipt, status_code=201)
async def omr_create_template(
    paper_id: UUID, body: s.TemplateCreate, db: DbSession, p: AuthorizedPrincipal
):
    return await service.create_template(db, p, paper_id, body)


@router.get(
    "/omr/templates/{template_id}/print.pdf",
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def omr_print_template(template_id: UUID, db: DbSession, p: AuthorizedPrincipal):
    return Response(
        await service.template_pdf(db, p, template_id),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="answer-sheet.pdf"'},
    )


@router.get("/answer-scripts/{script_id}/omr", response_model=s.CurrentRecognition)
async def omr_current(script_id: UUID, db: DbSession, p: AuthorizedPrincipal):
    return await service.current(db, p, script_id)


@router.post("/answer-scripts/{script_id}/omr", response_model=s.WriteReceipt)
async def omr_recognize(
    script_id: UUID,
    body: s.RecognitionCreate,
    db: DbSession,
    p: AuthorizedPrincipal,
    storage: Storage,
):
    return await service.recognize(db, p, storage, script_id, body)


@router.get("/omr/recognitions/{recognition_id}", response_model=s.RecognitionRead)
async def omr_inspect(recognition_id: UUID, db: DbSession, p: AuthorizedPrincipal):
    return await service.inspect(db, p, recognition_id)


@router.get(
    "/omr/recognitions/{recognition_id}/image",
    response_class=Response,
    responses={
        200: {"content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def omr_image(
    recognition_id: UUID,
    db: DbSession,
    p: AuthorizedPrincipal,
    storage: Storage,
    overlay: bool = False,
):
    return Response(
        await service.image(db, p, storage, recognition_id, overlay=overlay),
        media_type="image/jpeg",
    )


@router.post("/omr/recognitions/{recognition_id}/confirm", response_model=s.WriteReceipt)
async def omr_confirm(
    recognition_id: UUID,
    body: s.ConfirmRequest,
    db: DbSession,
    p: AuthorizedPrincipal,
    storage: Storage,
):
    return await service.confirm(db, p, storage, recognition_id, body)
