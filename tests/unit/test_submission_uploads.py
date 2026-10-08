"""Unit tests for bounded upload staging and file-signature checks."""

import hashlib
from io import BytesIO

import pytest
from starlette.datastructures import Headers, UploadFile

from huipi_cloud.modules.submissions.errors import (
    UnsupportedUploadError,
    UploadTooLargeError,
)
from huipi_cloud.modules.submissions.uploads import stage_upload

PDF_DATA = b"%PDF-1.7\nunit test\n%%EOF"


@pytest.mark.anyio
async def test_stage_upload_sanitizes_filename_and_calculates_sha256() -> None:
    upload = UploadFile(
        file=BytesIO(PDF_DATA),
        filename="../student/answer.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )
    staged = await stage_upload(upload, max_size_bytes=len(PDF_DATA))
    try:
        assert staged.filename == "answer.pdf"
        assert staged.size_bytes == len(PDF_DATA)
        assert staged.sha256 == hashlib.sha256(PDF_DATA).hexdigest()
        assert staged.fileobj.read() == PDF_DATA
    finally:
        staged.fileobj.close()


@pytest.mark.anyio
async def test_stage_upload_rejects_file_over_configured_limit() -> None:
    upload = UploadFile(
        file=BytesIO(PDF_DATA),
        filename="answer.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )

    with pytest.raises(UploadTooLargeError):
        await stage_upload(upload, max_size_bytes=len(PDF_DATA) - 1)


@pytest.mark.anyio
async def test_stage_upload_rejects_mismatched_signature() -> None:
    upload = UploadFile(
        file=BytesIO(b"\x89PNG\r\n\x1a\nfake"),
        filename="answer.pdf",
        headers=Headers({"content-type": "application/pdf"}),
    )

    with pytest.raises(UnsupportedUploadError):
        await stage_upload(upload, max_size_bytes=1024)
