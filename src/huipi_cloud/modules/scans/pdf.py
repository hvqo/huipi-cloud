"""Bounded subprocess for hostile PDF decoding, reusing the existing PDF renderer."""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from huipi_cloud.modules.identity.protocol import AccessError


async def previews(root: Path, maximum: int, timeout: float) -> list[dict]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "huipi_cloud.modules.scans.pdf_child",
        str(root),
        str(maximum),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout):
            await process.wait()
        if process.returncode != 0:
            raise AccessError("scan_pdf_processing_failed", 400)
        manifest = root / "manifest.json"
        if not manifest.is_file() or manifest.stat().st_size > 64 * 1024:
            raise AccessError("scan_pdf_invalid", 400)
        data = json.loads(manifest.read_text())
        if error := data.get("error"):
            raise AccessError(error, 413 if error.endswith("limit") else 400)
        pages = data["pages"]
        if not 1 <= len(pages) <= maximum:
            raise AccessError("scan_pdf_invalid", 400)
        for index, page in enumerate(pages):
            if (
                page["page_index"] != index
                or not 1 <= page["width"] <= 1600
                or not (1 <= page["height"] <= 1600 and 0 < page["size_bytes"] <= 2 * 1024 * 1024)
            ):
                raise AccessError("scan_preview_limit", 413)
        return pages
    except TimeoutError:
        raise AccessError("scan_pdf_processing_timeout", 503) from None
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.wait_for(process.wait(), 5)
