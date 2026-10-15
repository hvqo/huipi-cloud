"""DeepSeek tool conversation adapter; shares existing bounded HTTP transport only."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

import httpx
from pydantic import ValidationError

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import _post_bounded
from huipi_cloud.modules.copilot.protocol import CopilotError, ModelReply, ToolCall, encode
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError


class CopilotProvider(Protocol):
    generation_config_digest: str

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply: ...


class DeepSeekCopilotProvider:
    """No SDK, local fallback, retry, grading JSON contract, or automatic tracing."""

    def __init__(self, config: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        if not (
            config.copilot_enabled
            and config.copilot_allow_remote
            and config.copilot_external_data_authorized
        ):
            raise CopilotError("remote_copilot_not_authorized")
        if config.copilot_base_url.rstrip("/") not in {
            "https://api.deepseek.com",
            "https://api.deepseek.com/v1",
        }:
            raise CopilotError("copilot_endpoint_not_permitted")
        key = config.copilot_api_key
        if not key or not key.get_secret_value().strip():
            raise CopilotError("copilot_api_key_missing")
        self.config = config
        self.transport = transport
        self._endpoint = config.copilot_base_url.rstrip("/") + "/chat/completions"
        self._headers = {
            "Authorization": "Bearer " + key.get_secret_value(),
            "Content-Type": "application/json",
        }
        safe_config = {
            k: v
            for k, v in config.model_dump(mode="json").items()
            if k.startswith("copilot_") and k != "copilot_api_key"
        }
        safe_config["thinking"] = "disabled"
        safe_config["temperature"] = 0
        self.generation_config_digest = hashlib.sha256(encode(safe_config).encode()).hexdigest()

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        body = {
            "model": self.config.copilot_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": self.config.copilot_max_output_tokens,
            "stream": False,
        }
        if not tools:
            body.pop("tools")
            body.pop("tool_choice")
        try:
            payload = await _post_bounded(
                self._endpoint,
                headers=self._headers,
                body=body,
                timeout_seconds=self.config.copilot_model_timeout_seconds,
                transport=self.transport,
            )
        except GradeProposalError as error:
            raise CopilotError(error.reason_code) from None
        except httpx.TransportError:
            raise CopilotError("provider_unavailable") from None
        try:
            outer = json.loads(payload)
            choice = outer["choices"][0]
            reason = choice["finish_reason"]
            if reason not in {"stop", "tool_calls"}:
                raise CopilotError(
                    "provider_response_truncated"
                    if reason == "length"
                    else "provider_finish_reason_invalid"
                )
            message = choice["message"]
            if message.get("role") != "assistant":
                raise ValueError
            calls = [
                ToolCall(
                    id=c["id"], name=c["function"]["name"], arguments=c["function"]["arguments"]
                )
                for c in message.get("tool_calls", [])
                if c["type"] == "function"
            ]
            if (
                len(calls) != len(message.get("tool_calls", []))
                or len({c.id for c in calls}) != len(calls)
                or (reason == "tool_calls") != bool(calls)
            ):
                raise ValueError
            usage = outer["usage"]
            result = ModelReply(
                model_id=outer["model"],
                content=message.get("content") or "",
                reasoning_content=message.get("reasoning_content"),
                tool_calls=calls,
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                cache_hit_tokens=usage.get("prompt_cache_hit_tokens"),
                cache_miss_tokens=usage.get("prompt_cache_miss_tokens"),
            )
            if not calls and not result.content.strip():
                raise ValueError
            return result
        except (KeyError, IndexError, TypeError, ValueError, ValidationError):
            raise CopilotError("provider_reply_invalid") from None
