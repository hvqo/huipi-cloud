from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class TaskTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    expected_task_id: UUID | None = None
    expected_revision: int = Field(default=0, ge=0, strict=True)


class Assignment(Intent):
    assignee_user_id: UUID
    targets: list[TaskTarget] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=256)
    force: bool = Field(default=False, strict=True)


class Transition(Intent):
    expected_revision: int = Field(ge=1, strict=True)
    reason: str = Field(default="人工操作", min_length=1, max_length=256)
