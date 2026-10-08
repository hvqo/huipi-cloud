"""Create the configured private local S3 bucket without deleting existing data."""

import sys

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from huipi_cloud.core.config import settings


def main() -> int:
    endpoint = settings.minio_endpoint_url
    access_key = settings.minio_access_key
    secret_key = settings.minio_secret_key
    if not endpoint or not access_key or not secret_key:
        print("请在 .env 中配置 MINIO_ENDPOINT_URL、MINIO_ACCESS_KEY 和 MINIO_SECRET_KEY")
        return 2

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=10,
        ),
    )
    try:
        try:
            client.head_bucket(Bucket=settings.minio_bucket)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            client.create_bucket(Bucket=settings.minio_bucket)
        client.put_bucket_acl(Bucket=settings.minio_bucket, ACL="private")
    except (BotoCoreError, ClientError):
        print("私有存储桶初始化失败；请检查本地对象存储服务与配置")
        return 1

    print(f"私有存储桶已就绪：{settings.minio_bucket}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
