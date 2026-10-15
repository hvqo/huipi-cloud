"""Strict, bounded local Copilot contracts; no persisted conversation or identity claim."""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from huipi_cloud.modules.knowledge.schemas import RetrievalHit

AGENT_VERSION = "teacher-copilot-retrieval-v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CopilotError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class CopilotRequest(StrictModel):
    assignment_id: UUID
    question: str = Field(min_length=1, max_length=2000)
    require_material_evidence: bool = Field(default=False, strict=True)

    @field_validator("question")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("question_blank")
        return value


class ToolCall(StrictModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_]+$")
    arguments: str = Field(max_length=2048)


class ModelReply(StrictModel):
    model_id: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=16000)
    reasoning_content: str | None = Field(default=None, max_length=16000)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=12)
    prompt_tokens: int = Field(ge=0, strict=True)
    completion_tokens: int = Field(ge=0, strict=True)
    cache_hit_tokens: int | None = Field(default=None, ge=0, strict=True)
    cache_miss_tokens: int | None = Field(default=None, ge=0, strict=True)


class TraceEvent(StrictModel):
    kind: Literal["model", "tool"]
    sequence: int
    name: str
    status: str
    elapsed_ms: int
    arguments_sha256: str | None = None
    result_sha256: str | None = None
    result_bytes: int = 0
    retrieval_mode: str | None = None
    degraded: bool | None = None
    degradation_reason: str | None = None
    model_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None


class CopilotResult(StrictModel):
    run_id: UUID = Field(default_factory=uuid4)
    agent_version: str = AGENT_VERSION
    generation_config_digest: str
    request_sha256: str
    status: Literal["completed", "stopped", "failed"] = "stopped"
    finish_reason: str = "not_started"
    answer: str = ""
    steps: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Reserve worst-case cost for attempts without confirmed usage too.
    reserved_cost_usd: str = "0"
    known_usage_cost_upper_usd: str = "0"
    elapsed_ms: int = 0
    trace: list[TraceEvent] = Field(default_factory=list)
    citations: list[RetrievalHit] = Field(default_factory=list)
    limitations: list[str] = Field(
        default_factory=lambda: [
            "local_operator_scope_is_not_authentication",
            "no_published_grades_available",
            "model_answer_requires_human_verification",
        ]
    )


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
