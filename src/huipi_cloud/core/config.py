"""Application settings loaded from environment variables or a local .env file."""

from typing import Self

from pydantic import Field, model_validator
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
    parsing_max_attempts: int = Field(default=3, ge=1, le=20)
    parsing_lease_seconds: int = Field(default=60, ge=3, le=3600)
    parsing_heartbeat_seconds: int = Field(default=15, ge=1, le=120)
    parsing_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    parsing_retry_base_seconds: int = Field(default=5, ge=1, le=3600)
    parsing_retry_max_seconds: int = Field(default=300, ge=1, le=86400)
    parsing_shutdown_grace_seconds: int = Field(default=30, ge=0, le=3600)
    parsing_cancel_grace_seconds: float = Field(default=5.0, ge=0, le=60)
    parsing_execution_timeout_seconds: float = Field(default=1800.0, gt=0, le=86400)
    parsing_executor: str | None = None
    mineru_executable: str = "mineru-kit"
    mineru_home: str | None = None
    mineru_tier: str = "basic"
    mineru_max_pdf_pages: int = Field(default=200, ge=1, le=10000)
    mineru_max_output_bytes: int = Field(default=1024 * 1024 * 1024, ge=1024 * 1024)
    mineru_max_text_bytes: int = Field(default=64 * 1024 * 1024, ge=1024 * 1024)
    mineru_max_archive_members: int = Field(default=10000, ge=1, le=100000)

    @model_validator(mode="after")
    def validate_parsing_timing(self) -> Self:
        if self.parsing_heartbeat_seconds >= self.parsing_lease_seconds:
            raise ValueError("PARSING_HEARTBEAT_SECONDS must be less than PARSING_LEASE_SECONDS")
        if self.parsing_retry_base_seconds > self.parsing_retry_max_seconds:
            raise ValueError("PARSING_RETRY_BASE_SECONDS cannot exceed PARSING_RETRY_MAX_SECONDS")
        if self.mineru_tier != "basic":
            raise ValueError("当前只支持经过本地ONNX模型验证的MINERU_TIER=basic")
        return self


settings = Settings()
