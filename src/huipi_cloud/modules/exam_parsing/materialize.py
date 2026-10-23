"""Stream verified private sources; assemble only the pinned ordered pages."""

import asyncio
import hashlib
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.scans.router import verified_download


async def assemble(storage, rows, key):
    async with asyncio.timeout(75):
        with tempfile.TemporaryDirectory(prefix="huipi-exam-input-") as name:
            root = Path(name)
            names = {}
            for row in rows:
                if row["file_id"] not in names:
                    path = await verified_download(
                        storage,
                        dict(
                            bucket=row["bucket"],
                            object_key=row["file_key"],
                            size_bytes=row["file_size"],
                            sha256=row["file_sha256"],
                        ),
                        32 * 1024**2,
                        root,
                    )
                    target = f"source-{len(names)}.pdf"
                    path.rename(root / target)
                    names[row["file_id"]] = target
                await verified_download(storage, row, 2 * 1024**2, root)
            (root / "selection.json").write_text(
                json.dumps([dict(file=names[r["file_id"]], index=r["page_index"]) for r in rows])
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "huipi_cloud.modules.exam_parsing.materialize_child",
                str(root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env={
                    "PATH": os.defpath,
                    "HOME": str(root),
                    "LANG": "C.UTF-8",
                    "PYTHONUNBUFFERED": "1",
                },
            )
            try:
                async with asyncio.timeout(40):
                    await process.wait()
                if process.returncode != 0:
                    raise AccessError("exam_parse_assembly_failed", 503)
            finally:
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(process.wait(), 5)
            path = root / "assembled.pdf"
            size = path.stat().st_size
            if not 0 < size <= 32 * 1024**2:
                raise AccessError("exam_parse_input_limit", 413)
            sha = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    sha.update(chunk)
                source.seek(0)
                await storage.upload_fileobj(source, key, "application/pdf")
            value = dict(
                bucket=storage.bucket, object_key=key, size_bytes=size, sha256=sha.hexdigest()
            )
            await verified_download(storage, value, 32 * 1024**2, root)
            return value
