"""Resource limits and process cleanup without PostgreSQL or model calls."""

import asyncio
import signal
import sys

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from huipi_cloud.api.v1.upload_limits import UploadRequestLimitMiddleware
from huipi_cloud.core.config import Settings
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.scans import pdf
from huipi_cloud.modules.scans.schemas import BatchCreate
from huipi_cloud.modules.submissions.errors import UploadTooLargeError


@pytest.mark.anyio
async def test_scan_admission_is_bounded_and_released_after_cancellation():
    entered, release = asyncio.Event(), asyncio.Event()
    active = 0
    app = FastAPI()

    @app.post("/api/v1/scan-batches/{batch}/files")
    async def held():
        nonlocal active
        active += 1
        if active == 2:
            entered.set()
        await release.wait()
        return {"ok": True}

    limited = UploadRequestLimitMiddleware(app, max_upload_size_bytes=100, api_v1_prefix="/api/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(limited), base_url="http://test"
    ) as c:
        tasks = [asyncio.create_task(c.post("/api/v1/scan-batches/b/files")) for _ in range(2)]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            rejected = await c.post("/api/v1/scan-batches/b/files")
            assert rejected.status_code == 429 and rejected.json() == {"detail": "scan_busy"}
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert limited.active_scan_uploads == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert limited.active_scan_uploads == 0
        assert (await c.post("/api/v1/scan-batches/b/files")).status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize("declared", [True, False])
async def test_scan_multipart_cap_handles_chunked_body_and_declared_length(declared):
    app = FastAPI()
    calls = []

    @app.exception_handler(UploadTooLargeError)
    async def too_large(request, error):
        return JSONResponse({"detail": "upload_too_large"}, status_code=413)

    @app.post("/api/v1/scan-batches/{batch}/files")
    async def consume(request: Request):
        calls.append(await request.body())
        return Response()

    async def stream():
        yield b"1234567"
        yield b"890123"

    limited = UploadRequestLimitMiddleware(
        app,
        max_upload_size_bytes=100,
        api_v1_prefix="/api/v1",
        scan_max_file_bytes=8,
        multipart_overhead_bytes=4,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(limited), base_url="http://test"
    ) as c:
        value = b"1234567890123" if declared else stream()
        result = await c.post("/api/v1/scan-batches/b/files", content=value)
        assert result.status_code == 413 and not calls
        assert limited.active_scan_uploads == 0


@pytest.mark.anyio
async def test_render_timeout_kills_and_reaps_actual_subprocess(tmp_path, monkeypatch):
    native_spawn = asyncio.create_subprocess_exec
    processes = []

    async def controlled_spawn(*args, **kwargs):
        process = await native_spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", controlled_spawn)
    with pytest.raises(AccessError) as failure:
        await pdf.previews(tmp_path, 50, 0.05)
    assert failure.value.code == "scan_pdf_processing_timeout"
    assert len(processes) == 1 and processes[0].returncode == -signal.SIGKILL


def test_config_caps_and_input_do_not_accept_client_classification():
    from pydantic import ValidationError

    cfg = Settings(_env_file=None)
    assert cfg.scan_max_file_bytes == 32 * 1024 * 1024 and cfg.scan_max_pages == 50
    with pytest.raises(ValidationError):
        Settings(_env_file=None, scan_max_pages=1600)
    with pytest.raises(ValidationError):
        BatchCreate(
            request_id="00000000-0000-4000-8000-000000000001",
            title="测试",
            security_classification="legacy_homework",
        )
