"""Application settings loaded from environment variables or a local .env file."""

from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
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
    canonical_max_middle_json_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)
    canonical_max_manifest_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    canonical_max_document_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=1024,
        le=64 * 1024 * 1024,
    )
    canonical_max_nodes: int = Field(default=50000, ge=1, le=1000000)
    canonical_max_nesting_depth: int = Field(default=64, ge=1, le=256)
    answer_alignment_max_document_bytes: int = Field(
        default=32 * 1024 * 1024,
        ge=1024,
        le=64 * 1024 * 1024,
    )
    answer_alignment_max_question_response_bytes: int = Field(
        default=2 * 1024 * 1024,
        ge=1024,
        le=8 * 1024 * 1024,
    )
    visual_evidence_enabled: bool = False
    visual_evidence_provider: Literal["openai_compatible", "ollama_native"] = (
        "openai_compatible"
    )
    visual_evidence_base_url: str = "http://127.0.0.1:11434/v1"
    visual_evidence_model: str = "qwen3.5:4b"
    visual_evidence_api_key: SecretStr | None = None
    visual_evidence_allow_remote: bool = False
    visual_evidence_external_data_authorized: bool = False
    visual_evidence_timeout_seconds: float = Field(default=90.0, gt=0, le=300)
    visual_evidence_max_retries: int = Field(default=1, ge=0, le=3)
    visual_evidence_max_output_tokens: int = Field(default=1200, ge=64, le=4096)
    visual_evidence_max_pages_per_question: int = Field(default=5, ge=1, le=20)
    visual_evidence_max_original_bytes: int = Field(
        default=20 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024
    )
    visual_evidence_max_source_pdf_pages: int = Field(default=200, ge=1, le=500)
    visual_evidence_max_image_pixels: int = Field(
        default=12_000_000, ge=1_000_000, le=24_000_000
    )
    visual_evidence_render_dpi: int = Field(default=120, ge=36, le=180)
    visual_evidence_max_render_edge: int = Field(default=2048, ge=512, le=4096)
    visual_evidence_max_page_image_bytes: int = Field(
        default=3 * 1024 * 1024, ge=64 * 1024, le=8 * 1024 * 1024
    )
    visual_evidence_max_total_input_bytes: int = Field(
        default=12 * 1024 * 1024, ge=256 * 1024, le=24 * 1024 * 1024
    )
    visual_evidence_max_proposal_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1024, le=4 * 1024 * 1024
    )

    @model_validator(mode="after")
    def validate_parsing_timing(self) -> Self:
        if self.parsing_heartbeat_seconds >= self.parsing_lease_seconds:
            raise ValueError("PARSING_HEARTBEAT_SECONDS must be less than PARSING_LEASE_SECONDS")
        if self.parsing_retry_base_seconds > self.parsing_retry_max_seconds:
            raise ValueError("PARSING_RETRY_BASE_SECONDS cannot exceed PARSING_RETRY_MAX_SECONDS")
        if self.mineru_tier != "basic":
            raise ValueError("当前只支持经过本地ONNX模型验证的MINERU_TIER=basic")
        if not self.visual_evidence_model.strip():
            raise ValueError("VISUAL_EVIDENCE_MODEL cannot be blank")
        return self


settings = Settings()
