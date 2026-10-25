"""Production selection is opt-in, single-call, exact and fail closed."""

import json

import pytest
from test_grading_prompts_v4 import CaptureProvider
from test_grading_proposals import _grade_input_document
from test_quote_selection import _selection_reply

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider
from huipi_cloud.modules.grading import prompts_selection, prompts_v4
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError
from huipi_cloud.modules.grading.proposal_service import (
    _call_and_validate,
    _request_fingerprint,
    production_prompt_version,
)


def config():
    return Settings(_env_file=None, grading_strategy="quote_selection")


def test_default_and_frozen_prompt_contracts():
    doc = _grade_input_document(["😀学生：x=4", "同义表达"])
    cfg = Settings(_env_file=None)
    assert cfg.grading_strategy == "strict_offsets"
    assert production_prompt_version(cfg) == prompts_v4.PROMPT_VERSION
    assert production_prompt_version(config()) == prompts_selection.PROMPT_VERSION
    old = json.loads(prompts_v4.build_user_prompt(doc))
    new = json.loads(prompts_selection.build_user_prompt(doc))
    for field in ["question", "teacher_answer_key", "rubric", "student_answer_segments"]:
        assert new[field] == old[field]
    assert "原" not in new["response_contract"]["evidence_references"]
    assert "text_start" not in json.dumps(new["response_contract"])
    assert set(new["response_contract"]["evidence_references"]) == {
        "quote_selection",
        "no_evidence",
    }
    provider = CaptureProvider({})
    assert _request_fingerprint(doc, provider) != _request_fingerprint(
        doc, provider, prompt_version=prompts_selection.PROMPT_VERSION
    )


def test_strategy_and_effective_zero_retries_participate_in_digest():
    base = Settings(
        _env_file=None,
        grading_enabled=True,
        grading_provider="ollama_native",
        grading_base_url="http://127.0.0.1:11434",
        grading_model="test-only",
    )
    strict = build_text_grading_provider(base)
    selection = build_text_grading_provider(
        base.model_copy(update={"grading_strategy": "quote_selection"})
    )
    zero = build_text_grading_provider(
        base.model_copy(
            update={
                "grading_strategy": "quote_selection",
                "grading_invalid_response_retries": 0,
            }
        )
    )
    assert strict.generation_config_digest != selection.generation_config_digest
    assert zero.generation_config_digest == selection.generation_config_digest


@pytest.mark.anyio
async def test_unicode_conversion_and_private_audit_callback():
    doc = _grade_input_document(["甲😀x≤2，保留  空格", "其他片段"])
    raw = _selection_reply(doc).model_dump(mode="json")
    raw["criterion_scores"][0]["evidence_references"][0]["quote"] = "x≤2"
    provider = CaptureProvider(raw)
    receipts = []

    async def audit(response, **kwargs):
        receipts.append((response, kwargs))

    reply, model, usage, _ = await _call_and_validate(
        provider, doc, config=config(), audit_response=audit
    )
    ref = reply.criterion_scores[0].evidence_references[0]
    assert (ref.kind, ref.text_start, ref.text_end) == ("quote", 2, 5)
    assert model == provider.model_id and usage == (10, 10)
    assert receipts[0][0].content == json.dumps(raw, ensure_ascii=False)
    assert receipts[0][1]["converted"] == reply
    assert receipts[0][1]["error_code"] is None
    assert len(provider.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,reason",
    [
        ("duplicate", "quote_ambiguous"),
        ("missing", "quote_not_found"),
        ("wrong_segment", "quote_not_found"),
        ("cross_segment", "quote_not_found"),
        ("trim", "quote_not_found"),
        ("no_quote", "positive_score_without_quote"),
        ("over_score", "criterion_score_out_of_range"),
        ("total", "model_total_mismatch"),
        ("extra_no_evidence", "model_evidence_protocol_invalid"),
        ("offsets", "model_evidence_protocol_invalid"),
    ],
)
async def test_invalid_selection_never_repaired_retried_or_partially_accepted(failure, reason):
    doc = _grade_input_document(["甲😀x=4，x=4", "另页乙"])
    raw = _selection_reply(doc).model_dump(mode="json")
    score = raw["criterion_scores"][0]
    ref = score["evidence_references"][0]
    if failure == "duplicate":
        ref["quote"] = "x=4"
    elif failure == "missing":
        ref["quote"] = "标准答案"
    elif failure == "wrong_segment":
        ref["segment_index"] = 1
    elif failure == "cross_segment":
        ref["quote"] += "另页乙"
    elif failure == "trim":
        ref["quote"] = "  " + ref["quote"]
    elif failure == "no_quote":
        score["evidence_references"] = [dict(kind="no_evidence", reason="未见步骤")]
    elif failure == "over_score":
        score["awarded_points"] = "99.00"
    elif failure == "total":
        raw["total_score"] = "0.00"
    elif failure == "extra_no_evidence":
        score["evidence_references"] = [dict(kind="no_evidence", reason="缺少", segment_index=0)]
    else:
        ref["text_start"] = 0
    before = json.dumps(raw, ensure_ascii=False)
    provider = CaptureProvider(raw)
    audits = []

    async def audit(response, **kwargs):
        audits.append(kwargs)

    with pytest.raises(GradeProposalError, match=reason):
        await _call_and_validate(provider, doc, config=config(), audit_response=audit)
    assert len(provider.requests) == 1  # config default retries=1 cannot sample again
    assert len(audits) == 1 and audits[0]["converted"] is None
    assert audits[0]["error_code"] == reason
    assert json.dumps(raw, ensure_ascii=False) == before


@pytest.mark.anyio
async def test_abstention_is_scoreless_and_no_automatic_fallback():
    doc = _grade_input_document()
    raw = _selection_reply(doc, mode="abstain").model_dump(mode="json")
    provider = CaptureProvider(raw)
    reply, _, _, _ = await _call_and_validate(provider, doc, config=config())
    assert reply.outcome == "abstain" and reply.total_score is None
    assert not reply.criterion_scores and len(provider.requests) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["json", "timeout", "truncated"])
async def test_provider_failures_do_not_change_strategy_or_retry(failure):
    from huipi_cloud.infrastructure.grading.provider import (
        GradeProviderEndedResponseError,
        ProviderUsage,
        TextGradingResponse,
    )
    from huipi_cloud.modules.grading.proposal_errors import GradeProposalProviderTimeoutError

    doc = _grade_input_document()

    class Broken(CaptureProvider):
        async def complete(self, **kwargs):
            self.requests.append(kwargs)
            if failure == "timeout":
                raise GradeProposalProviderTimeoutError()
            response = TextGradingResponse("{", self.model_id, 5, ProviderUsage(10, 4), "length")
            if failure == "truncated":
                raise GradeProviderEndedResponseError("model_output_truncated", response)
            return response

    provider = Broken({})
    audits = []

    async def audit(response, **kwargs):
        audits.append((response, kwargs))

    with pytest.raises(GradeProposalError):
        await _call_and_validate(provider, doc, config=config(), audit_response=audit)
    assert len(provider.requests) == 1
    assert len(audits) == (0 if failure == "timeout" else 1)
    if audits:
        assert audits[0][0].content == "{" and audits[0][1]["converted"] is None


def test_round_counter_is_shared_and_enforces_thirteen_requests(tmp_path):
    from scripts.validate_production_quote_selection import reserve_call

    for _ in range(13):
        reserve_call(tmp_path)
    with pytest.raises(RuntimeError, match="round_request_limit"):
        reserve_call(tmp_path)
    assert json.loads((tmp_path / "call-counter.json").read_text())["attempted"] == 13


@pytest.mark.anyio
async def test_frozen_real_responses_replay_without_network_or_label_changes():
    import hashlib
    from pathlib import Path

    from huipi_cloud.modules.grading.evaluation import build_grade_input, load_dataset

    root = Path(__file__).parents[2] / "data/evaluation/grading"
    folder = root / "production-quote-selection-20261025"
    dataset, digest = load_dataset(root / "deepseek-prompt-v4-smoke-v1.json")
    assert digest == "43215a6deae4bc8f144dbeb9e5b65dfc9186dda76274860e489192c748d6d69b"
    for checksum in folder.glob("*.sha256"):
        data = checksum.with_suffix("")
        assert hashlib.sha256(data.read_bytes()).hexdigest() == checksum.read_text().split()[0]
    predictions = json.loads((folder / "predictions.json").read_text())
    assert len(predictions["cases"]) == 12
    cases = {c.case_id: c for c in dataset.cases}
    for record in predictions["cases"]:
        provider = CaptureProvider(json.loads(record["response"]["content"]))
        doc = build_grade_input(cases[record["case_id"]])
        assert doc.input_sha256 == record["input_sha256"]
        reply, _, _, _ = await _call_and_validate(provider, doc, config=config())
        assert reply.model_dump(mode="json") == record["converted_reply"]
        prompt = json.loads(provider.requests[0]["user_prompt"])
        assert "reference_total_score" not in prompt and "reference_criterion_scores" not in prompt
    old_path = root / "deepseek-prompt-v4-smoke-predictions-v1.json"
    assert (
        hashlib.sha256(old_path.read_bytes()).hexdigest()
        == predictions["preflight"]["baseline_sha256"]
    )
    old = json.loads(old_path.read_text())
    assert sum(r["status"] == "candidate" for r in old["cases"]) == 7
    diagnostics = json.loads((folder / "offline-feasibility.json").read_text())
    invalid = [r for r in diagnostics["references"] if r["original_quote_error"]]
    assert len(invalid) == 5 and all(r["status"] == "unique_exact" for r in invalid)
    assert diagnostics["historical_statuses_unchanged"] is True
    fraction = cases["fraction-partial"]
    assert str(fraction.reference_total_score) == "7.00"  # model gave 10; never relabel it
