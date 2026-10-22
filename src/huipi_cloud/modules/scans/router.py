"""Authenticated session-scoped PDF intake and private source/preview bytes."""

import asyncio
import hashlib
import tempfile
from contextlib import aclosing
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import Response as ByteResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.submissions.router import Storage, _content_disposition

from . import service
from .schemas import BatchCreate, BatchPage, BatchRead, FilePage, FileRead, PagePage, SessionPage


class ScanRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                result = await handler(request)
                result.headers["Cache-Control"] = "private, no-store"
                result.headers["X-Content-Type-Options"] = "nosniff"
                return result
            except (SQLAlchemyError, OSError, TimeoutError):
                raise AccessError("scan_unavailable", 503) from None

        return safe


router = APIRouter(
    tags=["exam-scans"],
    route_class=ScanRoute,
    responses={
        404: {"description": "资源不存在或没有场次授权"},
        409: {"description": "重复文件、处理中或请求冲突"},
        413: {"description": "文件、页数或预览资源超过上限"},
        429: {"description": "扫描接收繁忙，请稍后重试"},
        503: {"description": "服务不可用或提交结果未知"},
    },
)
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=100000)]


@router.get("/scan-sessions", response_model=SessionPage)
async def list_scan_sessions(
    session: DbSession, principal: AuthorizedPrincipal, offset: Offset = 0, limit: Limit = 20
):
    return await service.sessions(session, principal, offset, limit)


@router.post(
    "/exam-sessions/{exam_session_id}/scan-batches", response_model=BatchRead, status_code=201
)
async def create_scan_batch(
    exam_session_id: UUID, body: BatchCreate, session: DbSession, principal: AuthorizedPrincipal
):
    return await service.create_batch(session, principal, exam_session_id, body)


@router.get("/exam-sessions/{exam_session_id}/scan-batches", response_model=BatchPage)
async def list_scan_batches(
    exam_session_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: Offset = 0,
    limit: Limit = 20,
):
    return await service.batches(session, principal, exam_session_id, offset, limit)


@router.get("/scan-batches/{batch_id}", response_model=BatchRead)
async def inspect_scan_batch(batch_id: UUID, session: DbSession, principal: AuthorizedPrincipal):
    return await service.inspect_batch(session, principal, batch_id)


@router.get("/scan-batches/{batch_id}/files", response_model=FilePage)
async def list_scan_files(
    batch_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: Offset = 0,
    limit: Limit = 20,
):
    return await service.files(session, principal, batch_id, offset, limit)


@router.post("/scan-batches/{batch_id}/files", response_model=FileRead, status_code=201)
async def upload_scan_file(
    batch_id: UUID,
    session: DbSession,
    storage: Storage,
    principal: AuthorizedPrincipal,
    response: Response,
    request_id: Annotated[UUID, Form()],
    file: Annotated[UploadFile, File()],
):
    result, replay = await service.upload(session, principal, storage, batch_id, request_id, file)
    if replay:
        response.status_code = 200
    return result


@router.get("/scan-files/{file_id}", response_model=FileRead)
async def inspect_scan_file(file_id: UUID, session: DbSession, principal: AuthorizedPrincipal):
    return await service.file_info(session, principal, file_id)


@router.get("/scan-files/{file_id}/pages", response_model=PagePage)
async def list_scan_pages(
    file_id: UUID,
    session: DbSession,
    principal: AuthorizedPrincipal,
    offset: Offset = 0,
    limit: Limit = 20,
):
    return await service.page_list(session, principal, file_id, offset, limit)


async def verified_download(storage, value, maximum, root):
    if value["bucket"] != storage.bucket:
        raise AccessError("scan_storage_location_changed", 503)
    if value["size_bytes"] > maximum:
        raise AccessError("scan_download_limit", 413)
    digest, size = hashlib.sha256(), 0
    path = root / "download"
    async with asyncio.timeout(45):
        original = await storage.download(value["object_key"])
        async with aclosing(original.chunks()) as chunks:
            with path.open("wb") as target:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > min(maximum, value["size_bytes"]):
                        raise AccessError("scan_source_integrity_failed", 503)
                    digest.update(chunk)
                    await asyncio.to_thread(target.write, chunk)
    if size != value["size_bytes"] or digest.hexdigest() != value["sha256"]:
        raise AccessError("scan_source_integrity_failed", 503)
    return path


@router.get(
    "/scan-files/{file_id}/original",
    response_class=FileResponse,
    responses={
        200: {"content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def download_scan_original(
    file_id: UUID, session: DbSession, principal: AuthorizedPrincipal, storage: Storage
):
    value = await service.download_metadata(session, principal, fid=file_id)
    directory = tempfile.TemporaryDirectory(prefix="huipi-scan-download-")
    try:
        path = await verified_download(storage, value, 32 * 1024 * 1024, Path(directory.name))
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={"Content-Disposition": _content_disposition(value["original_filename"])},
            background=BackgroundTask(directory.cleanup),
        )
    except BaseException:
        directory.cleanup()
        raise


@router.get(
    "/scan-pages/{page_id}/preview",
    response_class=ByteResponse,
    responses={
        200: {"content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def preview_scan_page(
    page_id: UUID, session: DbSession, principal: AuthorizedPrincipal, storage: Storage
):
    value = await service.download_metadata(session, principal, pid=page_id)
    with tempfile.TemporaryDirectory(prefix="huipi-scan-preview-") as directory:
        path = await verified_download(storage, value, 2 * 1024 * 1024, Path(directory))
        return ByteResponse(path.read_bytes(), media_type="image/jpeg")
