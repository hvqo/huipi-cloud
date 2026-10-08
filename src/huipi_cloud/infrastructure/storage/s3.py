"""S3-compatible private object storage with blocking SDK calls offloaded to threads."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.response import StreamingBody


class StorageUnavailableError(Exception):
    """Raised when the configured object storage cannot complete an operation."""


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
            )
        except (BotoCoreError, ClientError, OSError) as error:
            raise StorageUnavailableError from error

    async def download(self, object_key: str) -> DownloadedObject:
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self.bucket,
                Key=object_key,
            )
        except (BotoCoreError, ClientError, OSError) as error:
            raise StorageUnavailableError from error
        return DownloadedObject(body=response["Body"])

    async def delete(self, object_key: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_object,
                Bucket=self.bucket,
                Key=object_key,
            )
        except (BotoCoreError, ClientError, OSError) as error:
            raise StorageUnavailableError from error
