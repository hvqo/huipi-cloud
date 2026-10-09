"""Boundaries for cancellation while boto3 reads a parser-owned file."""

import asyncio
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from boto3.s3.transfer import TransferConfig

from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage


class BlockingUploadClient:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.fileobj = None
        self.payload: bytes | None = None

    def upload_fileobj(self, fileobj, bucket, key, *, ExtraArgs, Config) -> None:
        del bucket, key, ExtraArgs, Config
        self.fileobj = fileobj
        self.started.set()
        if not self.release.wait(timeout=3):
            raise TimeoutError("test upload was not released")
        self.payload = fileobj.read()
        self.finished.set()


@pytest.mark.anyio
async def test_cancelled_path_upload_keeps_thread_owned_file_open(tmp_path: Path) -> None:
    client = BlockingUploadClient()
    storage = object.__new__(S3ObjectStorage)
    storage.bucket = "test-bucket"
    storage._client = client
    storage._transfer_config = TransferConfig(max_concurrency=1, use_threads=False)
    storage._path_upload_lock = threading.Lock()
    storage._path_upload_thread = None
    path = tmp_path / "artifact.zip"
    payload = b"controlled parser output"
    path.write_bytes(payload)

    upload = asyncio.create_task(
        storage.upload_path(path, "parsed/run/artifact.zip", "application/zip")
    )
    assert await asyncio.wait_for(asyncio.to_thread(client.started.wait, 2), timeout=3)
    upload.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(upload, timeout=0.5)

    assert client.fileobj is not None
    assert not client.fileobj.closed
    path.unlink()
    assert not path.exists()

    client.release.set()
    assert await asyncio.wait_for(asyncio.to_thread(client.finished.wait, 2), timeout=3)
    assert client.payload == payload
    assert client.fileobj.closed


def test_blocked_daemon_upload_does_not_hold_worker_event_loop_open(tmp_path: Path) -> None:
    path = tmp_path / "artifact.zip"
    path.write_bytes(b"worker shutdown probe")
    script = r"""
import asyncio, sys, threading
from pathlib import Path
from boto3.s3.transfer import TransferConfig
from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage

class Client:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
    def upload_fileobj(self, fileobj, bucket, key, *, ExtraArgs, Config):
        self.started.set()
        self.release.wait(60)

client = Client()
storage = object.__new__(S3ObjectStorage)
storage.bucket = "test-bucket"
storage._client = client
storage._transfer_config = TransferConfig(max_concurrency=1, use_threads=False)
storage._path_upload_lock = threading.Lock()
storage._path_upload_thread = None

async def main():
    task = asyncio.create_task(
        storage.upload_path(Path(sys.argv[1]), "test/key", "application/zip")
    )
    await asyncio.wait_for(asyncio.to_thread(client.started.wait), timeout=2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

asyncio.run(main())
print("worker-loop-exited")
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert result.stdout.strip() == "worker-loop-exited"
