"""Tests for early multipart request-size limits."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from huipi_cloud.api.v1.upload_limits import UploadRequestLimitMiddleware
from huipi_cloud.modules.submissions.errors import UploadTooLargeError


class OversizedChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"1234567"
        yield b"890123"


@pytest.mark.anyio
async def test_upload_request_limit_rejects_stream_without_content_length() -> None:
    application = FastAPI()

    @application.exception_handler(UploadTooLargeError)
    async def handle_upload_too_large(
        _request: Request,
        error: UploadTooLargeError,
    ) -> JSONResponse:
        return JSONResponse(status_code=413, content={"detail": str(error)})

    @application.post("/api/v1/assignments/{assignment_id}/submissions")
    async def consume_body(request: Request) -> Response:
        return Response(content=str(len(await request.body())))

    limited_application = UploadRequestLimitMiddleware(
        application,
        max_upload_size_bytes=8,
        api_v1_prefix="/api/v1",
        multipart_overhead_bytes=4,
    )
    transport = httpx.ASGITransport(app=limited_application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/assignments/00000000-0000-0000-0000-000000000001/submissions",
            content=OversizedChunkStream(),
            headers={"Content-Type": "multipart/form-data; boundary=test"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "文件超过大小限制"}


@pytest.mark.anyio
async def test_student_import_upload_uses_its_own_smaller_pre_limit() -> None:
    application = FastAPI()

    @application.exception_handler(UploadTooLargeError)
    async def handle_upload_too_large(
        _request: Request,
        error: UploadTooLargeError,
    ) -> JSONResponse:
        return JSONResponse(status_code=413, content={"detail": str(error)})

    @application.post("/api/v1/organization/student-imports")
    async def receive_import(request: Request) -> Response:
        return Response(content=str(len(await request.body())))

    limited_application = UploadRequestLimitMiddleware(
        application,
        max_upload_size_bytes=64,
        api_v1_prefix="/api/v1",
        multipart_overhead_bytes=4,
        student_import_max_bytes=8,
    )
    transport = httpx.ASGITransport(app=limited_application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        too_large = await client.post(
            "/api/v1/organization/student-imports",
            content=OversizedChunkStream(),
            headers={"Content-Type": "multipart/form-data; boundary=test"},
        )
        accepted = await client.post(
            "/api/v1/organization/student-imports",
            content=b"12345678",
            headers={"Content-Type": "multipart/form-data; boundary=test"},
        )

    assert too_large.status_code == 413
    assert accepted.status_code == 200
    assert accepted.text == "8"


def _limited_student_import_app(*, student_import_max_bytes: int = 8) -> FastAPI:
    application = FastAPI()
    calls: list[int] = []

    @application.exception_handler(UploadTooLargeError)
    async def handle_upload_too_large(
        _request: Request,
        error: UploadTooLargeError,
    ) -> JSONResponse:
        return JSONResponse(status_code=413, content={"detail": str(error)})

    @application.post("/api/v1/organization/student-imports")
    async def receive_import(request: Request) -> Response:
        calls.append(len(await request.body()))
        return Response(content=str(calls[-1]))

    @application.post("/api/v1/organization/student-imports/")
    async def receive_import_trailing_slash(request: Request) -> Response:
        return Response(content=str(len(await request.body())))

    application.state.calls = calls
    return UploadRequestLimitMiddleware(
        application,
        max_upload_size_bytes=64,
        api_v1_prefix="/api/v1",
        multipart_overhead_bytes=4,
        student_import_max_bytes=student_import_max_bytes,
    )


@pytest.mark.anyio
async def test_student_import_pre_limit_rejects_declared_content_length() -> None:
    limited_application = _limited_student_import_app()
    transport = httpx.ASGITransport(app=limited_application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/organization/student-imports",
            content=b"0123456789abc",
        )

    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    assert limited_application.app.state.calls == []


@pytest.mark.anyio
async def test_student_import_pre_limit_ignores_other_methods_and_trailing_slash() -> None:
    limited_application = _limited_student_import_app()
    transport = httpx.ASGITransport(app=limited_application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        trailing = await client.post(
            "/api/v1/organization/student-imports/",
            content=b"0123456789",
        )
        put = await client.put(
            "/api/v1/organization/student-imports",
            content=b"0123456789",
        )

    # Only the exact POST upload path is pre-limited; other shapes never spool this body
    # through the upload handler and are rejected by routing/auth layers.
    assert trailing.status_code == 200
    assert trailing.text == "10"
    assert put.status_code == 405
