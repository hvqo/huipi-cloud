"""Real PG/S3 flows with MockTransport; two separately opted-in paid smoke tests."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import OpenAICompatibleTextGradingProvider
from huipi_cloud.infrastructure.visual_evidence.provider import OpenAICompatibleVisionProvider
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.grading.evaluation import _CaptureProvider
from huipi_cloud.modules.grading.models import GradeProposalArtifact
from huipi_cloud.modules.grading.proposal_errors import (
    GradeProposalError,
    GradeProposalIdempotencyConflictError,
)
from huipi_cloud.modules.grading.proposal_service import (
    grade_input_artifact,
    read_grade_proposal_artifact,
)
from huipi_cloud.modules.visual_evidence.errors import (
    VisualEvidenceConflictError,
    VisualEvidenceError,
)
from huipi_cloud.modules.visual_evidence.models import VisualEvidenceArtifact
from huipi_cloud.modules.visual_evidence.protocol import VisualEvidenceProposal
from huipi_cloud.modules.visual_evidence.service import analyze_question_visual_evidence

INTEGRATION = Path(__file__).resolve().parent
if str(INTEGRATION) not in sys.path:
    sys.path.insert(0, str(INTEGRATION))
from test_canonical_documents import _prepare_visual_submission  # noqa: E402
from test_grade_proposals import _ready_grade_input  # noqa: E402


def _mock_config() -> Settings:
    return Settings(
        grading_enabled=True,
        grading_provider="openai_compatible",
        grading_base_url="https://api.deepseek.com",
        grading_model="deepseek-flash",
        grading_api_key=SecretStr("synthetic-test-key"),
        grading_allow_remote=True,
        grading_external_data_authorized=True,
        grading_invalid_response_retries=0,
        grading_deepseek_thinking="disabled",
        grading_max_output_tokens=4096,
        visual_evidence_enabled=True,
        visual_evidence_provider="openai_compatible",
        visual_evidence_base_url="https://api.deepseek.com",
        visual_evidence_model="deepseek-flash",
        visual_evidence_api_key=SecretStr("synthetic-test-key"),
        visual_evidence_allow_remote=True,
        visual_evidence_external_data_authorized=True,
        visual_evidence_max_retries=0,
        visual_evidence_deepseek_thinking="disabled",
        visual_evidence_max_output_tokens=2048,
    )


def _envelope(reply, model, finish="stop"):
    return {
        "model": model,
        "choices": [{"finish_reason": finish, "message": {"content": json.dumps(reply)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


async def _review_count(factory):
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(AnswerReviewDecision))


@pytest.mark.anyio
@pytest.mark.parametrize("fault", ["none", "truncated", "bad_quote", "no_evidence"])
async def test_deepseek_http_to_real_pg_s3_grading_preserves_evidence_gates(
    submission_client,
    recording_minio_storage,
    postgres_engine,
    fault,
):
    factory, _, _, _, prepared, document, reply = await _ready_grade_input(
        submission_client, recording_minio_storage, postgres_engine
    )
    if fault == "bad_quote":
        reply["criterion_scores"][0]["evidence_references"][0]["quote"] = "x" * len(
            reply["criterion_scores"][0]["evidence_references"][0]["quote"]
        )
    elif fault == "no_evidence":
        reply["criterion_scores"][0]["evidence_references"] = [
            {"kind": "no_evidence", "reason": "缺失可计分证据。"}
        ]
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert "reasoning_effort" not in body
        return httpx.Response(
            200,
            json=_envelope(reply, "deepseek-flash", "length" if fault == "truncated" else "stop"),
        )

    config = _mock_config()
    provider = OpenAICompatibleTextGradingProvider(config, transport=httpx.MockTransport(handler))
    before = await _review_count(factory)
    request_id = uuid4()
    kwargs = dict(
        grade_input_artifact_id=prepared.artifact_id,
        request_id=request_id,
        config=config,
        provider=provider,
    )
    if fault != "none":
        with pytest.raises(GradeProposalError) as raised:
            await grade_input_artifact(factory, recording_minio_storage, **kwargs)
        assert (
            raised.value.reason_code
            == {
                "truncated": "provider_response_truncated",
                "bad_quote": "evidence_quote_mismatch",
                "no_evidence": "positive_score_without_quote",
            }[fault]
        )
        async with factory() as session:
            assert (
                await session.scalar(select(func.count()).select_from(GradeProposalArtifact)) == 0
            )
        assert not any(
            "/grading/proposals/" in key for key in recording_minio_storage.uploaded_keys
        )
    else:
        result = await grade_input_artifact(factory, recording_minio_storage, **kwargs)
        assert result.status == "completed_candidate" and result.total_score == 10
        proposal, sha = await read_grade_proposal_artifact(
            factory, recording_minio_storage, result.proposal_id, config=config
        )
        assert proposal.model_id == "deepseek-flash"
        assert proposal.grade_input_sha256 == document.input_sha256
        assert proposal.provider == "openai_compatible"
        assert proposal.prompt_tokens == 100 and proposal.completion_tokens == 50
        assert len(sha) == 64
        replay = await grade_input_artifact(factory, recording_minio_storage, **kwargs)
        assert replay.proposal_id == result.proposal_id and replay.created is False
        async with factory() as session:
            row = await session.get(GradeProposalArtifact, result.proposal_id)
        payload = b"".join(
            [
                chunk
                async for chunk in (await recording_minio_storage.download(row.object_key)).chunks()
            ]
        )
        assert len(payload) == row.object_size_bytes
        assert hashlib.sha256(payload).hexdigest() == sha
        changed_config = config.model_copy(update={"grading_deepseek_thinking": "enabled"})
        changed_provider = OpenAICompatibleTextGradingProvider(
            changed_config, transport=httpx.MockTransport(handler)
        )
        with pytest.raises(GradeProposalIdempotencyConflictError):
            await grade_input_artifact(
                factory,
                recording_minio_storage,
                grade_input_artifact_id=prepared.artifact_id,
                request_id=request_id,
                config=changed_config,
                provider=changed_provider,
            )
    assert calls == 1
    assert await _review_count(factory) == before


def _visual_reply(page=0):
    return {
        "outcome": "candidate_response_present",
        "visual_regions": [
            {
                "page_index": page,
                "visual_bbox": [0.1, 0.1, 0.9, 0.25],
                "evidence_type": "handwritten_text",
                "reason_codes": ["handwriting_visible"],
            }
        ],
        "reason_codes": ["visual_evidence_candidate_only"],
    }


async def _visual_source(client, storage, engine):
    image = (INTEGRATION.parent / "fixtures/mineru/synthetic-image.png").read_bytes()
    return await _prepare_visual_submission(
        client,
        storage,
        engine,
        middle_content="第1题 学生提交的合成答案",
        middle_bbox=(0.1, 0.1, 0.9, 0.25),
        source_override=("synthetic.png", image, "image/png"),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("fault", ["none", "truncated", "unsubmitted_page"])
async def test_deepseek_image_http_to_real_pg_s3_and_mapping_is_candidate_only(
    submission_client,
    recording_minio_storage,
    postgres_engine,
    fault,
):
    submission_id, question_id, factory = await _visual_source(
        submission_client, recording_minio_storage, postgres_engine
    )
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert body["thinking"] == {"type": "disabled"}
        images = [block for block in body["messages"][1]["content"] if block["type"] == "image_url"]
        assert len(images) == 1 and images[0]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"
        )
        return httpx.Response(
            200,
            json=_envelope(
                _visual_reply(99 if fault == "unsubmitted_page" else 0),
                "deepseek-flash-test-resolved-id",
                "length" if fault == "truncated" else "stop",
            ),
        )

    config = _mock_config()
    provider = OpenAICompatibleVisionProvider(config, transport=httpx.MockTransport(handler))
    before = await _review_count(factory)
    kwargs = dict(
        submission_id=submission_id,
        question_id=question_id,
        request_id=uuid4(),
        config=config,
    )
    if fault != "none":
        with pytest.raises(VisualEvidenceError) as raised:
            await analyze_question_visual_evidence(
                factory, recording_minio_storage, provider, **kwargs
            )
        assert (
            raised.value.code
            == {
                "truncated": "provider_response_truncated",
                "unsubmitted_page": "model_referred_to_unsubmitted_page",
            }[fault]
        )
        async with factory() as session:
            assert (
                await session.scalar(select(func.count()).select_from(VisualEvidenceArtifact)) == 0
            )
    else:
        row = await analyze_question_visual_evidence(
            factory, recording_minio_storage, provider, **kwargs
        )
        payload = b"".join(
            [
                chunk
                async for chunk in (
                    await recording_minio_storage.download(row.proposal_object_key)
                ).chunks()
            ]
        )
        assert hashlib.sha256(payload).hexdigest() == row.proposal_sha256
        assert len(payload) == row.proposal_size_bytes
        proposal = VisualEvidenceProposal.model_validate_json(payload)
        assert proposal.model_id == "deepseek-flash-test-resolved-id"
        assert proposal.visual_regions[0].attribution_status == "candidate"
        assert proposal.visual_regions[0].candidate_content_pointer
        assert proposal.provider_usage.total_tokens == 150
        replay = await analyze_question_visual_evidence(
            factory, recording_minio_storage, provider, **kwargs
        )
        assert replay.id == row.id
        changed_config = config.model_copy(update={"visual_evidence_deepseek_thinking": "enabled"})
        changed_provider = OpenAICompatibleVisionProvider(
            changed_config, transport=httpx.MockTransport(handler)
        )
        with pytest.raises(VisualEvidenceConflictError):
            await analyze_question_visual_evidence(
                factory,
                recording_minio_storage,
                changed_provider,
                **{**kwargs, "config": changed_config},
            )
    assert calls == 1 and await _review_count(factory) == before


def _real_config() -> Settings:
    config = Settings()
    assert config.grading_allow_remote and config.grading_external_data_authorized, (
        "在本地.env中显式配置合成文本远程传输授权"
    )
    assert (
        config.visual_evidence_allow_remote and config.visual_evidence_external_data_authorized
    ), "在本地.env中显式配置合成图像远程传输授权"
    assert config.grading_api_key and config.visual_evidence_api_key, "本地.env缺少API Key"
    return config.model_copy(
        update={
            "grading_enabled": True,
            "grading_provider": "openai_compatible",
            "grading_base_url": "https://api.deepseek.com",
            "grading_model": "deepseek-flash",
            "grading_deepseek_thinking": "disabled",
            "grading_max_output_tokens": 4096,
            "grading_timeout_seconds": 120,
            "grading_invalid_response_retries": 0,
            "visual_evidence_enabled": True,
            "visual_evidence_provider": "openai_compatible",
            "visual_evidence_base_url": "https://api.deepseek.com",
            "visual_evidence_model": "deepseek-flash",
            "visual_evidence_deepseek_thinking": "disabled",
            "visual_evidence_max_output_tokens": 2048,
            "visual_evidence_timeout_seconds": 90,
            "visual_evidence_max_retries": 0,
        }
    )


@pytest.mark.anyio
@pytest.mark.deepseek_e2e
@pytest.mark.skipif(os.getenv("RUN_DEEPSEEK_SMOKE") != "1", reason="未授权按需真实DeepSeek合成验收")
async def test_real_deepseek_synthetic_text_grading_pg_s3_smoke(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    config = _real_config()
    factory, _, _, _, prepared, _, _ = await _ready_grade_input(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        middle_content="第1题 计算题干：2+2。4",
        response_text="4",
    )
    before = await _review_count(factory)
    provider = _CaptureProvider(OpenAICompatibleTextGradingProvider(config))
    result = await grade_input_artifact(
        factory,
        recording_minio_storage,
        grade_input_artifact_id=prepared.artifact_id,
        request_id=uuid4(),
        config=config,
        provider=provider,
    )
    assert result.status == "completed_candidate"
    proposal, sha = await read_grade_proposal_artifact(
        factory, recording_minio_storage, result.proposal_id, config=config
    )
    assert await _review_count(factory) == before
    print(
        "DEEPSEEK_TEXT_SMOKE="
        + json.dumps(
            {
                "actual_model_id": proposal.model_id,
                "finish_reason": provider.finish_reason,
                "status": result.status,
                "prompt_tokens": provider.prompt_tokens,
                "completion_tokens": provider.completion_tokens,
                "reasoning_tokens": provider.reasoning_tokens,
                "latency_ms": provider.elapsed_ms,
                "proposal_sha256_verified": sha,
                "provider_requests": 1,
                "input_scope": "synthetic_only",
            },
            ensure_ascii=False,
        )
    )


@pytest.mark.anyio
@pytest.mark.deepseek_e2e
@pytest.mark.skipif(os.getenv("RUN_DEEPSEEK_SMOKE") != "1", reason="未授权按需真实DeepSeek合成验收")
async def test_real_deepseek_synthetic_image_pg_s3_smoke(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    config = _real_config()
    submission_id, question_id, factory = await _visual_source(
        submission_client, recording_minio_storage, postgres_engine
    )
    before = await _review_count(factory)
    row = await analyze_question_visual_evidence(
        factory,
        recording_minio_storage,
        OpenAICompatibleVisionProvider(config),
        submission_id=submission_id,
        question_id=question_id,
        request_id=uuid4(),
        config=config,
    )
    payload = b"".join(
        [
            chunk
            async for chunk in (
                await recording_minio_storage.download(row.proposal_object_key)
            ).chunks()
        ]
    )
    assert hashlib.sha256(payload).hexdigest() == row.proposal_sha256
    proposal = VisualEvidenceProposal.model_validate_json(payload)
    assert await _review_count(factory) == before
    print(
        "DEEPSEEK_VISUAL_SMOKE="
        + json.dumps(
            {
                "actual_model_id": proposal.model_id,
                "outcome": proposal.outcome,
                "mapping_statuses": [r.attribution_status for r in proposal.visual_regions],
                "provider_usage": proposal.provider_usage.model_dump()
                if proposal.provider_usage
                else None,
                "latency_ms": proposal.model_elapsed_ms,
                "provider_requests": 1,
                "canonical_source": "synthetic Canonical/Alignment fixture; not real MinerU",
                "input_scope": "synthetic_only",
            },
            ensure_ascii=False,
        )
    )
