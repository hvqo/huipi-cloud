"""Bounded temporary staging and validation for one multipart upload."""

import asyncio
import hashlib
import tempfile
from dataclasses import dataclass
from typing import BinaryIO

from fastapi import UploadFile

from huipi_cloud.modules.submissions.errors import (
    InvalidUploadError,
    UnsupportedUploadError,
    UploadTooLargeError,
)

CHUNK_SIZE = 64 * 1024
SPOOL_MEMORY_LIMIT = 1024 * 1024
ALLOWED_TYPES = {
    ".pdf": ("application/pdf",),
    ".jpg": ("image/jpeg",),
    ".jpeg": ("image/jpeg",),
    ".png": ("image/png",),
}


@dataclass
class StagedUpload:
    fileobj: BinaryIO
    filename: str
    content_type: str
    size_bytes: int
    sha256: str


def _safe_filename(raw_filename: str | None) -> str:
    if not raw_filename or any(ord(char) < 32 or ord(char) == 127 for char in raw_filename):
        raise InvalidUploadError("文件名无效")
    filename = raw_filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1].strip()
    if not filename or len(filename) > 255:
        raise InvalidUploadError("文件名无效")
    return filename


def _signature_matches(extension: str, prefix: bytes) -> bool:
    if extension == ".pdf":
        return b"%PDF-" in prefix[:1024]
    if extension in {".jpg", ".jpeg"}:
        return prefix.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    return False


async def stage_upload(upload: UploadFile, max_size_bytes: int) -> StagedUpload:
    """Read in bounded chunks to a spooled temporary file and validate its signature."""
    filename = _safe_filename(upload.filename)
    extension = "." + filename.rsplit(".", maxsplit=1)[-1].lower() if "." in filename else ""
    expected_types = ALLOWED_TYPES.get(extension)
    declared_type = (upload.content_type or "").lower()
    if expected_types is None or declared_type not in expected_types:
        raise UnsupportedUploadError("仅支持 PDF、JPG/JPEG 和 PNG 文件")

    fileobj = tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_LIMIT, mode="w+b")
    digest = hashlib.sha256()
    prefix = bytearray()
    size = 0
    try:
        while chunk := await upload.read(CHUNK_SIZE):
            size += len(chunk)
            if size > max_size_bytes:
                raise UploadTooLargeError("文件超过大小限制")
            if len(prefix) < 1024:
                prefix.extend(chunk[: 1024 - len(prefix)])
            digest.update(chunk)
            await asyncio.to_thread(fileobj.write, chunk)

        if size == 0:
            raise InvalidUploadError("不能上传空文件")
        if not _signature_matches(extension, bytes(prefix)):
            raise UnsupportedUploadError("文件扩展名、声明类型与文件内容不匹配")
        await asyncio.to_thread(fileobj.seek, 0)
        return StagedUpload(
            fileobj=fileobj,
            filename=filename,
            content_type=expected_types[0],
            size_bytes=size,
            sha256=digest.hexdigest(),
        )
    except BaseException:
        await asyncio.to_thread(fileobj.close)
        raise
