"""Deterministic visual contract; never calls a remote model."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr, ValidationError

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import OpenAICompatibleTextGradingProvider
from huipi_cloud.modules.exam_grading.visual_protocol import (
    VisualGradeInput,
    VisualGradeInputCore,
    VisualModelReply,
    VisualRegion,
)
from huipi_cloud.modules.exam_grading.visual_scoring import validate_candidate
from huipi_cloud.modules.exam_grading.visual_selection import crop
from huipi_cloud.modules.grading.proposal_errors import (
    GradeProposalInvalidEvidenceError,
    GradeProposalInvalidRubricScoresError,
    GradeProposalProviderError,
)
from huipi_cloud.modules.grading.protocol import (
    AnswerKeySnapshot,
    GradeInputQuestionSnapshot,
    RubricCriterionSnapshot,
    json_sha256,
    rubric_sha256,
)
from huipi_cloud.modules.identity.protocol import AccessError


def image_bytes():
    value = BytesIO()
    Image.new("RGB", (200, 240), "white").save(value, format="PNG")
    return value.getvalue()


def document():
    qid, answer_id, criterion_id = uuid4(), uuid4(), uuid4()
    rubric = [
        RubricCriterionSnapshot(
            criterion_id=criterion_id,
            question_id=qid,
            description="解释计算",
            points=Decimal("5.00"),
            sort_order=1,
        )
    ]
    sha = "a" * 64
    region = VisualRegion(
        region_index=0,
        scan_page_id=uuid4(),
        scan_file_id=uuid4(),
        source_page_index=0,
        page_sha256=sha,
        crop_sha256=hashlib.sha256(image_bytes()).hexdigest(),
        width=200,
        height=240,
        x0=0,
        y0=0,
        x1=200,
        y1=240,
    )
    core = VisualGradeInputCore(
        request_id=uuid4(),
        selection_id=uuid4(),
        selection_revision=1,
        selection_sha256=json_sha256([region]),
        submission_id=uuid4(),
        assignment_id=uuid4(),
        question=GradeInputQuestionSnapshot(
            question_id=qid,
            question_number=1,
            question_type="short_answer",
            stem="计算",
            max_score=Decimal("5.00"),
        ),
        canonical_artifact_id=uuid4(),
        canonical_sha256=sha,
        alignment_artifact_id=uuid4(),
        alignment_sha256=sha,
        assignment_questions_digest=sha,
        source_file_sha256=sha,
        answer_key=AnswerKeySnapshot(
            answer_key_id=answer_id,
            question_id=qid,
            source="teacher",
            answer_content="4",
            sha256=hashlib.sha256(b"4").hexdigest(),
        ),
        rubric_snapshot=rubric,
        rubric_sha256=rubric_sha256(rubric),
        regions=[region],
    )
    return VisualGradeInput(
        **core.model_dump(),
        artifact_id=uuid4(),
        input_sha256=json_sha256(core),
        created_at=datetime.now(UTC),
    )


def reply(doc, *, score="5.00", total="5.00", region=0):
    return VisualModelReply.model_validate(
        {
            "schema_version": "1.0",
            "outcome": "candidate",
            "total_score": total,
            "criterion_scores": [
                {
                    "criterion_id": str(doc.rubric_snapshot[0].criterion_id),
                    "awarded_points": score,
                    "rationale": "所选区域显示计算",
                    "evidence_references": [
                        {"kind": "image_region", "region_index": region, "description": "可见计算"}
                    ],
                }
            ],
        }
    )


def test_visual_scores_require_real_region_and_exact_rubric_total():
    doc = document()
    scores, total = validate_candidate(reply(doc), doc, Settings(_env_file=None))
    assert total == Decimal("5.00") and scores[0].evidence_references[0].region_index == 0
    with pytest.raises(GradeProposalInvalidEvidenceError):
        validate_candidate(reply(doc, region=1), doc, Settings(_env_file=None))
    with pytest.raises(GradeProposalInvalidRubricScoresError):
        validate_candidate(reply(doc, score="6.00", total="6.00"), doc, Settings(_env_file=None))
    with pytest.raises(GradeProposalInvalidRubricScoresError):
        validate_candidate(reply(doc, total="4.00"), doc, Settings(_env_file=None))


def test_json_mode_marker_is_fixed_and_does_not_relax_other_fields():
    doc = document()
    raw = reply(doc).model_dump(mode="json", exclude_none=True)
    raw.pop("evidence_references")
    raw["type"] = "json_object"
    parsed = VisualModelReply.model_validate(raw)
    assert validate_candidate(parsed, doc, Settings(_env_file=None))[1] == Decimal("5.00")
    raw["type"] = "not_json"
    with pytest.raises(ValidationError):
        VisualModelReply.model_validate(raw)
    raw["type"] = "json_object"
    del raw["schema_version"]
    assert VisualModelReply.model_validate(raw).schema_version == "1.0"
    raw["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        VisualModelReply.model_validate(raw)
    raw["schema_version"] = "1.0"
    raw["unexpected"] = "not allowed"
    with pytest.raises(ValidationError):
        VisualModelReply.model_validate(raw)


def test_abstention_allows_only_empty_top_level_evidence():
    value = dict(
        type="json_object",
        outcome="abstain",
        total_score=None,
        criterion_scores=[],
        abstention_reason="图像模糊，无法可靠判断",
        evidence_references=[],
    )
    assert VisualModelReply.model_validate(value).outcome == "abstain"
    value["evidence_references"] = [
        {"kind": "image_region", "region_index": 0, "description": "不能读取"}
    ]
    with pytest.raises(ValidationError):
        VisualModelReply.model_validate(value)
    candidate = reply(document()).model_dump(mode="json", exclude_none=True)
    candidate["evidence_references"] = []
    with pytest.raises(ValidationError):
        VisualModelReply.model_validate(candidate)


def test_visual_crop_rejects_invalid_bounds_and_preserves_pixels():
    data = image_bytes()
    output, width, height = crop(data, (10, 20, 180, 220))
    assert output.startswith(b"\x89PNG") and (width, height) == (200, 240)
    with Image.open(BytesIO(output)) as image:
        assert image.size == (170, 200)
    with pytest.raises(AccessError):
        crop(data, (-1, 0, 100, 200))
    with pytest.raises(AccessError):
        crop(data, (10, 20, 180, 220), (201, 240))


@pytest.mark.anyio
async def test_deepseek_visual_transport_receives_actual_image_and_opt_in():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 5},
            },
        )

    config = Settings(
        _env_file=None,
        grading_enabled=True,
        grading_visual_enabled=True,
        grading_provider="openai_compatible",
        grading_model="deepseek-flash",
        grading_base_url="https://api.deepseek.com",
        grading_api_key=SecretStr("synthetic-test-key"),
        grading_allow_remote=True,
        grading_external_data_authorized=True,
        grading_deepseek_thinking="disabled",
    )
    provider = OpenAICompatibleTextGradingProvider(config, transport=httpx.MockTransport(handler))
    response = await provider.complete_visual(
        system_prompt="规则", user_prompt="题目", images=[image_bytes()]
    )
    assert response.model_id == "deepseek-flash" and response.usage.prompt_tokens == 200
    body = seen[0]
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    images = [p for p in body["messages"][1]["content"] if p["type"] == "image_url"]
    assert len(images) == 1 and images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    disabled = OpenAICompatibleTextGradingProvider(
        config.model_copy(update={"grading_visual_enabled": False}),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GradeProposalProviderError):
        await disabled.complete_visual(
            system_prompt="规则", user_prompt="题目", images=[image_bytes()]
        )
    assert len(seen) == 1
