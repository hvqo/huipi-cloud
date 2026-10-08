"""FastAPI dependency for the configured object storage adapter."""

from functools import lru_cache

from fastapi import HTTPException, status

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage


@lru_cache(maxsize=1)
def _configured_storage() -> S3ObjectStorage:
    endpoint = settings.minio_endpoint_url
    access_key = settings.minio_access_key
    secret_key = settings.minio_secret_key
    if not endpoint or not access_key or not secret_key:
        raise ValueError("MinIO configuration is incomplete")
    return S3ObjectStorage(
        endpoint_url=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=settings.minio_bucket,
        object_key_prefix=settings.minio_object_key_prefix,
    )


def get_object_storage() -> S3ObjectStorage:
    try:
        return _configured_storage()
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="对象存储尚未配置",
        ) from error
