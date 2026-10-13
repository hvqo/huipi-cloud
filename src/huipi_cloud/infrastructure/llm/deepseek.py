"""DeepSeek-specific request options, without another HTTP client or SDK."""

from urllib.parse import urlparse


def is_deepseek_endpoint(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "api.deepseek.com"
        and parsed.port in {None, 443}
    )


def deepseek_options(endpoint: str, thinking: str, effort: str) -> dict[str, object]:
    if not is_deepseek_endpoint(endpoint):
        return {}
    options: dict[str, object] = {"thinking": {"type": thinking}}
    # Effort can itself toggle thinking; do not send it when explicitly disabled.
    if thinking == "enabled":
        options["reasoning_effort"] = effort
    return options


def finish_reason_error(reason: object, *, required: bool = False) -> str | None:
    if reason == "stop" or (reason is None and not required):
        return None
    if reason == "length":
        return "provider_response_truncated"
    if reason is None:
        return "provider_finish_reason_missing"
    return "provider_generation_incomplete"
