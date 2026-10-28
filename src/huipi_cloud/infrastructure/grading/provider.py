"""Local-first text LLM adapters with bounded HTTP and response handling."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Protocol
from urllib.parse import urlparse

import httpx

from huipi_cloud.core.config import Settings, settings
from huipi_cloud.infrastructure.llm.deepseek import (
    deepseek_options,
    finish_reason_error,
    is_deepseek_endpoint,
)
from huipi_cloud.modules.grading.proposal_errors import (
    GradeProposalInvalidModelResponseError,
    GradeProposalProviderError,
    GradeProposalProviderRateLimitedError,
    GradeProposalProviderTimeoutError,
)

MAX_PROVIDER_RESPONSE_BYTES = 128 * 1024


@dataclass(frozen=True)
class ProviderUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None


@dataclass(frozen=True)
class TextGradingResponse:
    content: str
    model_id: str
    elapsed_ms: int
    usage: ProviderUsage
    finish_reason: str | None = None


class GradeProviderEndedResponseError(GradeProposalInvalidModelResponseError):
    """Preserve safe billing metadata and the raw final content for offline audit."""

    def __init__(self, reason_code: str, response: TextGradingResponse) -> None:
        super().__init__(reason_code)
        self.response = response


class TextGradingProvider(Protocol):
    provider_name: str
    model_id: str
    generation_config_digest: str

    async def complete(self, *, system_prompt: str, user_prompt: str) -> TextGradingResponse: ...


def build_text_grading_provider(config: Settings = settings) -> TextGradingProvider:
    config = effective_grading_config(config)
    if not config.grading_enabled:
        raise GradeProposalProviderError("grading_provider_disabled")
    if not config.grading_model.strip():
        raise GradeProposalProviderError("grading_model_not_configured")
    if config.grading_provider == "ollama_native":
        return OllamaTextGradingProvider(config)
    return OpenAICompatibleTextGradingProvider(config)


def effective_grading_config(config: Settings) -> Settings:
    # Selection is one model call; ambiguity must never trigger a new sample.
    if config.grading_strategy == "quote_selection":
        return config.model_copy(update={"grading_invalid_response_retries": 0})
    return config


class _HttpTextGradingProvider:
    provider_name: str

    def __init__(
        self,
        config: Settings,
        *,
        endpoint: str,
        transport: httpx.AsyncBaseTransport | None = None,
        native_ollama: bool,
    ) -> None:
        self._endpoint = endpoint
        self._model = config.grading_model.strip()
        self.model_id = self._model
        self._timeout = config.grading_timeout_seconds
        self._max_tokens = config.grading_max_output_tokens
        self._transport = transport
        self._native_ollama = native_ollama
        self._visual_enabled = config.grading_visual_enabled
        self._deepseek_options = (
            deepseek_options(
                endpoint,
                config.grading_deepseek_thinking,
                config.grading_deepseek_reasoning_effort,
            )
            if not native_ollama
            else {}
        )
        api_key = config.grading_api_key.get_secret_value() if config.grading_api_key else None
        if self._deepseek_options and (not api_key or not api_key.strip()):
            raise GradeProposalProviderError("deepseek_api_key_missing")
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self.generation_config_digest = hashlib.sha256(
            json.dumps(
                {
                    "provider": self.provider_name,
                    "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
                    "model": self._model,
                    "temperature": (
                        None if self._deepseek_options.get("thinking") == {"type": "enabled"} else 0
                    ),
                    **(
                        {
                            "deepseek_options": self._deepseek_options,
                            "configured_reasoning_effort": config.grading_deepseek_reasoning_effort,
                        }
                        if self._deepseek_options
                        else {}
                    ),
                    "max_output_tokens": self._max_tokens,
                    "max_input_tokens": config.grading_max_input_tokens,
                    "timeout_seconds": config.grading_timeout_seconds,
                    "invalid_response_retries": config.grading_invalid_response_retries,
                    **(
                        {"strategy": "quote_selection", "locator": "segment-unique-exact-v1"}
                        if config.grading_strategy == "quote_selection"
                        else {}
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    async def complete(self, *, system_prompt: str, user_prompt: str) -> TextGradingResponse:
        return await self._complete(system_prompt=system_prompt, user_prompt=user_prompt)

    async def complete_visual(
        self, *, system_prompt: str, user_prompt: str, images: list[bytes]
    ) -> TextGradingResponse:
        """Send only bounded, server-cropped PNGs; never accept an external image URL."""
        if (
            not self._visual_enabled
            or self._native_ollama
            or not is_deepseek_endpoint(self._endpoint)
            or self._model != "deepseek-flash"
        ):
            raise GradeProposalProviderError("visual_grading_not_enabled")
        if not 1 <= len(images) <= 8 or any(
            not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > 1024 * 1024
            for image in images
        ):
            raise GradeProposalProviderError("visual_grading_image_invalid")
        return await self._complete(
            system_prompt=system_prompt, user_prompt=user_prompt, images=images
        )

    async def _complete(
        self, *, system_prompt: str, user_prompt: str, images: list[bytes] | None = None
    ) -> TextGradingResponse:
        started = time.perf_counter()
        if self._native_ollama:
            body: dict[str, object] = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "think": False,
                "format": "json",
                "options": {"temperature": 0, "num_predict": self._max_tokens},
            }
        else:
            content: object = user_prompt
            if images:
                content = [{"type": "text", "text": user_prompt}]
                for index, image in enumerate(images):
                    content.extend(
                        [
                            {"type": "text", "text": f"作答图片区域 region_index={index}"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,"
                                    + base64.b64encode(image).decode("ascii"),
                                    "detail": "high",
                                },
                            },
                        ]
                    )
            body = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                "temperature": 0,
                "max_tokens": self._max_tokens,
                "response_format": {"type": "json_object"},
            }
            body.update(self._deepseek_options)
            if self._deepseek_options.get("thinking") == {"type": "enabled"}:
                body.pop("temperature")

        response_body = await _post_bounded(
            self._endpoint,
            headers=self._headers,
            body=body,
            timeout_seconds=self._timeout,
            transport=self._transport,
        )
        try:
            outer = json.loads(response_body)
            if self._native_ollama:
                content = outer["message"]["content"]
                response_model = outer.get("model")
                prompt_tokens = outer.get("prompt_eval_count")
                completion_tokens = outer.get("eval_count")
                finish_reason = outer.get("done_reason")
                usage = {}
            else:
                choice = outer["choices"][0]
                content = choice["message"]["content"]
                finish_reason = choice.get("finish_reason")
                response_model = outer.get("model")
                usage = outer.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
            if finish_reason is not None and not isinstance(finish_reason, str):
                raise ValueError
            reason = finish_reason_error(
                finish_reason, required=is_deepseek_endpoint(self._endpoint)
            )
            if content is None and reason is not None:
                content = ""
            if (
                not isinstance(content, str)
                or len(content.encode("utf-8")) > MAX_PROVIDER_RESPONSE_BYTES
            ):
                raise ValueError
            model_id = response_model if isinstance(response_model, str) else self._model
            if self._deepseek_options and not isinstance(response_model, str):
                raise ValueError
            if not model_id.strip() or len(model_id) > 200:
                raise ValueError
            details = usage.get("completion_tokens_details")
            details = details if isinstance(details, dict) else {}
            result = TextGradingResponse(
                content=content,
                model_id=model_id,
                elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
                usage=ProviderUsage(
                    prompt_tokens=_optional_nonnegative_int(prompt_tokens),
                    completion_tokens=_optional_nonnegative_int(completion_tokens),
                    reasoning_tokens=_optional_nonnegative_int(details.get("reasoning_tokens")),
                    cache_hit_tokens=_optional_nonnegative_int(
                        usage.get("prompt_cache_hit_tokens")
                    ),
                    cache_miss_tokens=_optional_nonnegative_int(
                        usage.get("prompt_cache_miss_tokens")
                    ),
                ),
                finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GradeProposalInvalidModelResponseError(
                "provider_response_envelope_invalid"
            ) from error
        if reason is not None:
            raise GradeProviderEndedResponseError(reason, result)
        return result


class OllamaTextGradingProvider(_HttpTextGradingProvider):
    """Ollama native `/api/chat` provider, restricted to a loopback endpoint."""

    provider_name = "ollama_native"

    def __init__(
        self,
        config: Settings = settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base = _validate_endpoint(config)
        parsed = urlparse(base)
        if parsed.scheme != "http" or parsed.path.rstrip("/") not in {"", "/v1"}:
            raise GradeProposalProviderError("ollama_native_endpoint_not_local")
        endpoint = f"{parsed.scheme}://{parsed.netloc}/api/chat"
        super().__init__(
            config,
            endpoint=endpoint,
            transport=transport,
            native_ollama=True,
        )


class OpenAICompatibleTextGradingProvider(_HttpTextGradingProvider):
    """OpenAI Chat Completion compatible provider with explicit remote consent."""

    provider_name = "openai_compatible"

    def __init__(
        self,
        config: Settings = settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base = _validate_endpoint(config)
        endpoint = base.rstrip("/") + "/chat/completions"
        super().__init__(
            config,
            endpoint=endpoint,
            transport=transport,
            native_ollama=False,
        )


def _validate_endpoint(config: Settings) -> str:
    raw = config.grading_base_url.strip()
    try:
        parsed = urlparse(raw)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or len(raw) > 2048
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
        ):
            raise ValueError
        try:
            local = ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = parsed.hostname.lower() == "localhost"
        if not local and not (
            config.grading_allow_remote
            and config.grading_external_data_authorized
            and parsed.scheme == "https"
            and config.grading_provider == "openai_compatible"
        ):
            raise ValueError
        return raw
    except (ValueError, TypeError) as error:
        raise GradeProposalProviderError("model_endpoint_not_permitted") from error


async def _post_bounded(
    endpoint: str,
    *,
    headers: dict[str, str],
    body: dict[str, object],
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> bytes:
    timeout = httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds))
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        ) as client:
            async with client.stream("POST", endpoint, headers=headers, json=body) as response:
                if response.status_code == 429:
                    raise GradeProposalProviderRateLimitedError("provider_rate_limited")
                if response.status_code in {408, 504}:
                    raise GradeProposalProviderTimeoutError("provider_timeout")
                if response.status_code >= 500:
                    raise GradeProposalProviderError("provider_unavailable")
                if response.status_code == 401:
                    raise GradeProposalProviderError("provider_authentication_failed")
                if response.status_code == 404:
                    raise GradeProposalProviderError("provider_model_or_endpoint_not_found")
                if response.status_code >= 400:
                    raise GradeProposalProviderError("provider_request_rejected")
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > MAX_PROVIDER_RESPONSE_BYTES:
                        raise GradeProposalInvalidModelResponseError(
                            "provider_response_size_limit_exceeded"
                        )
                    payload.extend(chunk)
                return bytes(payload)
    except (GradeProposalProviderError, GradeProposalInvalidModelResponseError):
        raise
    except httpx.TimeoutException as error:
        raise GradeProposalProviderTimeoutError("provider_timeout") from error
    except httpx.NetworkError as error:
        raise GradeProposalProviderError("provider_unavailable") from error


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None
