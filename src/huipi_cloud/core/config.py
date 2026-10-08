"""Application settings loaded from environment variables or a local .env file."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Basic settings needed to start the application."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "慧批云端"
    app_env: str = "development"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    database_url: str | None = None
    test_database_url: str | None = None
    minio_endpoint_url: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_bucket: str = "huipi-cloud"
    minio_test_bucket: str = "huipi-cloud-test"
    minio_object_key_prefix: str = ""
    max_upload_size_bytes: int = Field(default=20 * 1024 * 1024, gt=0)


settings = Settings()
