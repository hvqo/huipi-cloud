"""Local-first text LLM adapters with bounded HTTP and response handling."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Protocol
from urllib.parse import urlparse

import httpx

from huipi_cloud.core.config import Settings, settings
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


@dataclass(frozen=True)
class TextGradingResponse:
    content: str
    model_id: str
    elapsed_ms: int
    usage: ProviderUsage


class TextGradingProvider(Protocol):
    provider_name: str
    model_id: str
    generation_config_digest: str

    async def complete(self, *, system_prompt: str, user_prompt: str) -> TextGradingResponse: ...


def build_text_grading_provider(config: Settings = settings) -> TextGradingProvider:
    if not config.grading_enabled:
        raise GradeProposalProviderError("grading_provider_disabled")
    if not config.grading_model.strip():
        raise GradeProposalProviderError("grading_model_not_configured")
    if config.grading_provider == "ollama_native":
        return OllamaTextGradingProvider(config)
    return OpenAICompatibleTextGradingProvider(config)


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
        api_key = config.grading_api_key.get_secret_value() if config.grading_api_key else None
        self._headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self.generation_config_digest = hashlib.sha256(
            json.dumps(
                {
                    "provider": self.provider_name,
                    "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
                    "model": self._model,
                    "temperature": 0,
                    "max_output_tokens": self._max_tokens,
                    "max_input_tokens": config.grading_max_input_tokens,
                    "timeout_seconds": config.grading_timeout_seconds,
                    "invalid_response_retries": config.grading_invalid_response_retries,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    async def complete(self, *, system_prompt: str, user_prompt: str) -> TextGradingResponse:
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
            body = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
                "max_tokens": self._max_tokens,
                "response_format": {"type": "json_object"},
            }

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
            else:
                content = outer["choices"][0]["message"]["content"]
                response_model = outer.get("model")
                usage = outer.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
            if (
                not isinstance(content, str)
                or len(content.encode("utf-8")) > MAX_PROVIDER_RESPONSE_BYTES
            ):
                raise ValueError
            model_id = response_model if isinstance(response_model, str) else self._model
            if not model_id.strip() or len(model_id) > 200:
                raise ValueError
            return TextGradingResponse(
                content=content,
                model_id=model_id,
                elapsed_ms=max(0, round((time.perf_counter() - started) * 1000)),
                usage=ProviderUsage(
                    prompt_tokens=_optional_nonnegative_int(prompt_tokens),
                    completion_tokens=_optional_nonnegative_int(completion_tokens),
                ),
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GradeProposalInvalidModelResponseError(
                "provider_response_envelope_invalid"
            ) from error


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
