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
