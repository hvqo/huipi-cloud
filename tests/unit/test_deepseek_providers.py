"""DeepSeek compatibility tests use MockTransport and synthetic content only."""

import argparse
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from test_grading_evaluation import _dataset
from test_grading_proposals import _candidate_reply, _grade_input_document
from test_visual_evidence import _good_reply, _image_page

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import (
    GradeProviderEndedResponseError,
    OllamaTextGradingProvider,
    OpenAICompatibleTextGradingProvider,
)
from huipi_cloud.infrastructure.visual_evidence.provider import OpenAICompatibleVisionProvider
from huipi_cloud.modules.grading.evaluation import (
    create_predictions_document,
    evaluate_predictions,
    load_dataset,
    request_prediction,
)
from huipi_cloud.modules.grading.proposal_errors import (
    GradeProposalInvalidModelResponseError,
    GradeProposalProviderError,
)
from huipi_cloud.modules.grading.proposal_service import _call_and_validate, _request_fingerprint
from huipi_cloud.modules.visual_evidence.errors import (
    VisualEvidenceConfigurationError,
    VisualEvidenceProtocolError,
    VisualEvidenceProviderError,
)
from scripts.evaluate_grade_proposals import _cost_preflight


def test_clean_configuration_defaults_to_disabled_cloud_without_local_fallback(monkeypatch):
    for name in Settings.model_fields:
        if name.startswith(("grading_", "visual_evidence_")):
            monkeypatch.delenv(name.upper(), raising=False)
    config = Settings(_env_file=None)
    for prefix in ("grading", "visual_evidence"):
        assert getattr(config, prefix + "_provider") == "openai_compatible"
        assert getattr(config, prefix + "_base_url") == "https://api.deepseek.com"
        assert getattr(config, prefix + "_model") == "deepseek-flash"
        assert not getattr(config, prefix + "_enabled")
        assert not getattr(config, prefix + "_allow_remote")
        assert not getattr(config, prefix + "_external_data_authorized")
        assert getattr(config, prefix + "_api_key") is None
        assert getattr(config, prefix + "_deepseek_thinking") == "disabled"

    # Explicit cloud failures are returned; a builder never switches to Ollama.
    from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider

    with pytest.raises(GradeProposalProviderError, match="model_endpoint_not_permitted"):
        build_text_grading_provider(config.model_copy(update={"grading_enabled": True}))


def _config(**updates) -> Settings:
    values = {
        "_env_file": None,
        "grading_enabled": True,
        "grading_provider": "openai_compatible",
        "grading_base_url": "https://api.deepseek.com",
        "grading_model": "deepseek-flash",
        "grading_api_key": SecretStr("synthetic-test-key"),
        "grading_allow_remote": True,
        "grading_external_data_authorized": True,
        "grading_invalid_response_retries": 0,
        "grading_max_output_tokens": 4096,
        "visual_evidence_enabled": True,
        "visual_evidence_provider": "openai_compatible",
        "visual_evidence_base_url": "https://api.deepseek.com",
        "visual_evidence_model": "deepseek-flash",
        "visual_evidence_api_key": SecretStr("synthetic-test-key"),
        "visual_evidence_allow_remote": True,
        "visual_evidence_external_data_authorized": True,
        "visual_evidence_max_retries": 0,
        "visual_evidence_max_output_tokens": 2048,
    }
    values.update(updates)
    return Settings(**values)


def _envelope(content, *, model="deepseek-flash", finish="stop"):
    return {
        "model": model,
        "choices": [
            {
                "finish_reason": finish,
                "message": {"content": content, "reasoning_content": "PRIVATE_REASONING_NOT_SAVED"},
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "prompt_cache_hit_tokens": 30,
            "prompt_cache_miss_tokens": 70,
            "completion_tokens_details": {"reasoning_tokens": 20},
        },
    }


async def _invoke(provider, visual):
    if visual:
        return await provider.analyze(
            question_number=1,
            question_stem="合成题 2+2",
            source_regions=[],
            pages=[_image_page()],
        )
    return await provider.complete(system_prompt="仅输出JSON", user_prompt="合成数据")


@pytest.mark.anyio
@pytest.mark.parametrize("visual", [False, True])
@pytest.mark.parametrize("thinking", ["disabled", "enabled"])
async def test_deepseek_body_thinking_usage_actual_model_and_json(visual, thinking):
    captured = []
    document = _grade_input_document()
    content = _good_reply() if visual else _candidate_reply(document).model_dump_json()

    def handler(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        captured.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer synthetic-test-key"
        return httpx.Response(
            200, json=_envelope(content, model="deepseek-flash" if visual else "deepseek-flash")
        )

    config = _config(
        grading_deepseek_thinking=thinking,
        visual_evidence_deepseek_thinking=thinking,
        grading_deepseek_reasoning_effort="max",
        visual_evidence_deepseek_reasoning_effort="max",
    )
    cls = OpenAICompatibleVisionProvider if visual else OpenAICompatibleTextGradingProvider
    provider = cls(config, transport=httpx.MockTransport(handler))
    if visual:
        response = await _invoke(provider, True)
        assert response.reply.outcome == "candidate_response_present"
        assert response.model_id == "deepseek-flash"
    else:
        reply, actual_model, usage, elapsed = await _call_and_validate(
            provider, document, config=config
        )
        assert reply.outcome == "candidate"
        assert actual_model == "deepseek-flash"
        assert usage == (100, 50) and elapsed >= 0
    assert len(captured) == 1
    assert captured[0]["thinking"] == {"type": thinking}
    assert captured[0]["model"] == "deepseek-flash"
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert captured[0]["max_tokens"] == (2048 if visual else 4096)
    if thinking == "enabled":
        assert captured[0]["reasoning_effort"] == "max"
        assert "temperature" not in captured[0]
    else:
        assert "reasoning_effort" not in captured[0]
        assert captured[0]["temperature"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("visual", [False, True])
@pytest.mark.parametrize("host", ["other.example.test", "api.deepseek.com.example.test"])
async def test_other_compatible_services_never_receive_deepseek_parameters(visual, host):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_envelope(_good_reply() if visual else "{}"))

    config = _config(
        grading_base_url=f"https://{host}/v1",
        visual_evidence_base_url=f"https://{host}/v1",
        grading_deepseek_thinking="enabled",
        visual_evidence_deepseek_thinking="enabled",
    )
    cls = OpenAICompatibleVisionProvider if visual else OpenAICompatibleTextGradingProvider
    await _invoke(cls(config, transport=httpx.MockTransport(handler)), visual)
    assert "thinking" not in bodies[0] and "reasoning_effort" not in bodies[0]
    assert bodies[0]["temperature"] == 0


@pytest.mark.parametrize("visual", [False, True])
def test_remote_permissions_and_missing_keys_fail_before_http(visual):
    cls = OpenAICompatibleVisionProvider if visual else OpenAICompatibleTextGradingProvider
    error = VisualEvidenceConfigurationError if visual else GradeProposalProviderError
    prefix = "visual_evidence" if visual else "grading"
    for permission in ("allow_remote", "external_data_authorized"):
        with pytest.raises(error, match="model_endpoint_not_permitted"):
            cls(_config(**{f"{prefix}_{permission}": False}))
    with pytest.raises(error, match="deepseek_api_key_missing"):
        cls(_config(**{f"{prefix}_api_key": None}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("grading_deepseek_thinking", "enabled"),
        ("grading_max_output_tokens", 8192),
        ("grading_timeout_seconds", 180),
    ],
)
def test_grading_generation_parameters_change_request_fingerprint(field, value):
    document = _grade_input_document()
    a = OpenAICompatibleTextGradingProvider(_config())
    b = OpenAICompatibleTextGradingProvider(_config(**{field: value}))
    assert a.generation_config_digest != b.generation_config_digest
    assert _request_fingerprint(document, a) != _request_fingerprint(document, b)


def test_effort_and_visual_options_change_fingerprint():
    a = OpenAICompatibleTextGradingProvider(_config(grading_deepseek_thinking="enabled"))
    b = OpenAICompatibleTextGradingProvider(
        _config(grading_deepseek_thinking="enabled", grading_deepseek_reasoning_effort="high")
    )
    assert a.generation_config_digest != b.generation_config_digest
    base = OpenAICompatibleVisionProvider(_config()).configuration_fingerprint
    for field, value in (
        ("visual_evidence_deepseek_thinking", "enabled"),
        ("visual_evidence_max_output_tokens", 8192),
        ("visual_evidence_timeout_seconds", 180),
    ):
        assert (
            OpenAICompatibleVisionProvider(_config(**{field: value})).configuration_fingerprint
            != base
        )
    a = OpenAICompatibleVisionProvider(_config(visual_evidence_deepseek_thinking="enabled"))
    b = OpenAICompatibleVisionProvider(
        _config(
            visual_evidence_deepseek_thinking="enabled",
            visual_evidence_deepseek_reasoning_effort="high",
        )
    )
    assert a.configuration_fingerprint != b.configuration_fingerprint


@pytest.mark.anyio
@pytest.mark.parametrize("visual", [False, True])
@pytest.mark.parametrize(
    "fault", ["400", "401", "404", "429", "timeout", "length", "json", "model", "finish_type"]
)
async def test_deepseek_failure_states_are_safe_bounded_and_never_success(visual, fault):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if fault.isdigit():
            return httpx.Response(int(fault), json={"error": "PRIVATE_SERVER_MESSAGE"})
        if fault == "timeout":
            raise httpx.ReadTimeout("PRIVATE_SERVER_MESSAGE", request=request)
        if fault == "model":
            return httpx.Response(404, json={"error": "unknown model"})
        content = "{bad" if fault == "json" else (_good_reply() if visual else "{}")
        return httpx.Response(
            200,
            json=_envelope(
                content,
                finish=17 if fault == "finish_type" else "length" if fault == "length" else "stop",
            ),
        )

    cls = OpenAICompatibleVisionProvider if visual else OpenAICompatibleTextGradingProvider
    error = (
        (VisualEvidenceProviderError, VisualEvidenceProtocolError)
        if visual
        else (GradeProposalProviderError, GradeProposalInvalidModelResponseError)
    )
    provider = cls(_config(), transport=httpx.MockTransport(handler))
    if not visual and fault == "json":
        with pytest.raises(GradeProposalInvalidModelResponseError):
            await _call_and_validate(provider, _grade_input_document(), config=_config())
    else:
        with pytest.raises(error) as raised:
            await _invoke(provider, visual)
        assert "PRIVATE_SERVER_MESSAGE" not in str(raised.value)
    assert calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("content", ['{"schema_version":"1.0"}', None])
async def test_truncated_reply_keeps_usage_and_cannot_recount_as_candidate(content):
    provider = OpenAICompatibleTextGradingProvider(
        _config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_envelope(content, finish="length"))
        ),
    )
    with pytest.raises(GradeProviderEndedResponseError) as error:
        await _invoke(provider, False)
    assert error.value.reason_code == "provider_response_truncated"
    assert error.value.response.usage.reasoning_tokens == 20
    assert error.value.response.usage.cache_hit_tokens == 30
    dataset, digest = _dataset()
    prediction = await request_prediction(dataset.cases[0], provider, _config())
    assert prediction.status == "provider_error"
    assert prediction.finish_reason == "length"
    assert prediction.completion_tokens == 50 and prediction.reasoning_tokens == 20
    document = create_predictions_document(dataset, digest, provider, _config(), [prediction])
    before = hashlib.sha256(document.model_dump_json().encode()).hexdigest()
    report = evaluate_predictions(dataset, digest, document)
    assert report["counts"]["valid_candidate_cases"] == 0
    assert report["prediction_model"]["configured_reasoning_effort"] == "max"
    assert report["prediction_model"]["reasoning_effort"] is None
    assert report["provider_call_metadata"][0]["actual_model_id"] == "deepseek-flash"
    assert hashlib.sha256(document.model_dump_json().encode()).hexdigest() == before
    assert "PRIVATE_REASONING_NOT_SAVED" not in document.model_dump_json()


def test_deepseek_evaluation_cost_gate_rejects_missing_prices_and_small_budget():
    dataset, _ = _dataset()
    args = argparse.Namespace(
        input_usd_per_million=None, output_usd_per_million=None, max_estimated_cost_usd="0.50"
    )
    with pytest.raises(ValueError, match="requires_current_prices"):
        _cost_preflight(dataset.cases, _config(), args)
    args.input_usd_per_million, args.output_usd_per_million = "1", "2"
    args.max_estimated_cost_usd = "0.000001"
    with pytest.raises(ValueError, match="exceeds_budget"):
        _cost_preflight(dataset.cases, _config(), args)
    args.max_estimated_cost_usd = "1"
    cost = _cost_preflight(dataset.cases, _config(), args)
    assert cost["request_cap"] == 1 and cost["output_token_cap"] == 4096
    assert cost["preflight_estimate_usd"] != "0"


def test_existing_ollama_generation_digest_keeps_its_original_definition():
    config = Settings(
        _env_file=None,
        grading_enabled=True,
        grading_model="synthetic-local",
        grading_base_url="http://127.0.0.1:11434",
    )
    provider = OllamaTextGradingProvider(config)
    original = {
        "provider": "ollama_native",
        "endpoint_sha256": hashlib.sha256(b"http://127.0.0.1:11434/api/chat").hexdigest(),
        "model": "synthetic-local",
        "temperature": 0,
        "max_output_tokens": config.grading_max_output_tokens,
        "max_input_tokens": config.grading_max_input_tokens,
        "timeout_seconds": config.grading_timeout_seconds,
        "invalid_response_retries": config.grading_invalid_response_retries,
    }
    expected = hashlib.sha256(
        json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert provider.generation_config_digest == expected


def test_deepseek_v2_manifest_does_not_invent_predictions_or_reuse_qwen_outputs():
    root = Path(__file__).resolve().parents[2] / "data/evaluation/grading"
    dataset, digest = load_dataset(root / "rubric-cases-v2.json")
    path = root / "deepseek-v2-run-manifest.json"
    manifest = json.loads(path.read_bytes())
    assert manifest["dataset_sha256"] == digest
    assert manifest["provider_requests_sent"] == 0
    assert manifest["actual_text_model_id"] is None
    assert manifest["actual_visual_model_id"] is None
    assert manifest["actual_token_usage"] is None
    assert len(manifest["cases"]) == len(dataset.cases)
    assert sum(case["status"] == "not_requested" for case in manifest["cases"]) == 25
    assert all("response_content" not in case for case in manifest["cases"])
    assert path.with_suffix(".json.sha256").read_text().split()[0] == (
        hashlib.sha256(path.read_bytes()).hexdigest()
    )
