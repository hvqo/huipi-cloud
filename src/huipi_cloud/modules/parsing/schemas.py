"""Safe parsing task API schemas."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ParsingTaskStatusRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: UUID = Field(validation_alias="id")
    submission_id: UUID
    status: Literal["pending", "running", "retry_wait", "succeeded", "failed"]
    attempt_count: int
    max_attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    next_run_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
