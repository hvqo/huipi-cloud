"""S3-compatible private object storage with blocking SDK calls offloaded to threads."""

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.response import StreamingBody


class StorageUnavailableError(Exception):
    """Raised when the configured object storage cannot complete an operation."""


class StorageObjectNotFoundError(StorageUnavailableError):
    """Raised when a specific object does not exist in the configured bucket."""


@dataclass
class DownloadedObject:
    """Streaming response body returned by S3-compatible storage."""

    body: StreamingBody

    async def chunks(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        try:
            while chunk := await asyncio.to_thread(self.body.read, chunk_size):
                yield chunk
        except (BotoCoreError, ClientError, OSError) as error:
            raise StorageUnavailableError from error
        finally:
            await asyncio.to_thread(self.body.close)


class S3ObjectStorage:
    """Thin asynchronous adapter around boto3's blocking S3 client."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        object_key_prefix: str = "",
    ) -> None:
        self.bucket = bucket
        self.object_key_prefix = object_key_prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=3,
                read_timeout=30,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
        self._transfer_config = TransferConfig(max_concurrency=1, use_threads=False)
        self._path_upload_lock = threading.Lock()
        self._path_upload_thread: threading.Thread | None = None

    async def upload_fileobj(
        self,
        fileobj: BinaryIO,
        object_key: str,
        content_type: str,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._client.upload_fileobj,
                fileobj,
                self.bucket,
                object_key,
                ExtraArgs={"ContentType": content_type},
                Config=self._transfer_config,
            )
        except (BotoCoreError, ClientError, OSError) as error:
            raise StorageUnavailableError from error

    async def upload_path(
        self,
        path: Path,
        object_key: str,
        content_type: str,
    ) -> None:
        """Upload a file while opening/closing its handle inside the blocking thread.

        Cancelling this coroutine cannot stop a boto3 call already running in its
        thread. Keeping the file handle owned by that call prevents the parser's
        temporary-directory cleanup from closing a handle that boto3 is reading.
        The caller does not wait for the thread after cancellation; each S3 request
        still uses the client's connect/read timeout and bounded retry settings.
        """

        loop = asyncio.get_running_loop()
        completion: asyncio.Future[None] = loop.create_future()
        thread: threading.Thread

        def upload() -> None:
            failure: BaseException | None = None
            try:
                with path.open("rb") as fileobj:
                    self._client.upload_fileobj(
                        fileobj,
                        self.bucket,
                        object_key,
                        ExtraArgs={"ContentType": content_type},
                        Config=self._transfer_config,
                    )
            except (BotoCoreError, ClientError, OSError) as error:
                failure = StorageUnavailableError()
                failure.__cause__ = error
            except BaseException as error:
                failure = error
            finally:
                with self._path_upload_lock:
                    if self._path_upload_thread is threading.current_thread():
                        self._path_upload_thread = None

            def finish() -> None:
                if completion.cancelled():
                    return
                if failure is None:
                    completion.set_result(None)
                else:
                    completion.set_exception(failure)

            try:
                loop.call_soon_threadsafe(finish)
            except RuntimeError:
                # The event loop may close after lease loss. The daemon thread
                # must not keep the Worker process alive or call a closed loop.
                pass

        with self._path_upload_lock:
            if self._path_upload_thread is not None and self._path_upload_thread.is_alive():
                raise StorageUnavailableError("another parser upload is still active")
            thread = threading.Thread(
                target=upload,
                daemon=True,
                name="huipi-s3-path-upload",
            )
            self._path_upload_thread = thread
            try:
                thread.start()
            except RuntimeError as error:
                self._path_upload_thread = None
                raise StorageUnavailableError from error

        await completion

    async def download(self, object_key: str) -> DownloadedObject:
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self.bucket,
                Key=object_key,
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise StorageObjectNotFoundError from error
            raise StorageUnavailableError from error
        except (BotoCoreError, OSError) as error:
            raise StorageUnavailableError from error
        return DownloadedObject(body=response["Body"])

    async def head_object(self, object_key: str) -> dict[str, object]:
        """Return safe object metadata without reading the object body."""
        try:
            response = await asyncio.to_thread(
                self._client.head_object,
                Bucket=self.bucket,
                Key=object_key,
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise StorageObjectNotFoundError from error
            raise StorageUnavailableError from error
        except (BotoCoreError, OSError) as error:
            raise StorageUnavailableError from error
        return {
            "content_length": response.get("ContentLength"),
            "content_type": response.get("ContentType"),
            "etag": response.get("ETag"),
        }

    async def delete(self, object_key: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_object,
                Bucket=self.bucket,
                Key=object_key,
            )
        except (BotoCoreError, ClientError, OSError) as error:
            raise StorageUnavailableError from error
