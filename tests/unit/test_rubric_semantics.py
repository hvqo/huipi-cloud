"""Protocol, provenance and calculation checks; Fake replies do not measure model quality."""

import json
from pathlib import Path

import pytest

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import ProviderUsage, TextGradingResponse
from huipi_cloud.modules.grading.evaluation import build_grade_input, load_dataset
from huipi_cloud.modules.grading.proposal_errors import GradeProposalProviderTimeoutError
from huipi_cloud.modules.grading.quote_evaluation import (
    create_experiment_predictions,
    evaluate_experiment,
    experiment_prompts,
    request_experiment_prediction,
    unrequested_prediction,
)
from huipi_cloud.modules.grading.quote_selection import (
    QUOTE_SELECTION_PROMPT_VERSION,
    QUOTE_SELECTION_SYSTEM_PROMPT,
    build_quote_selection_prompt,
)
from huipi_cloud.modules.grading.quote_semantics import SEMANTICS_PROMPT_VERSION

DATA = Path(__file__).parents[2] / "data/evaluation/grading"


def reference_reply(case):
    doc = build_grade_input(case)
    if case.reference_status == "excluded_uncertain":
        return json.dumps(
            {
                "schema_version": "1.0",
                "schema_name": "huipi.grading.quote-selection",
                "outcome": "abstain",
                "abstention_reason": "必要来源不可核验。",
                "criterion_scores": [],
            },
            ensure_ascii=False,
        )
    refs = {s.criterion_key: s for s in case.reference_criterion_scores}
    scores = []
    for idx, rub in enumerate(case.rubric):
        ref = refs[rub.criterion_key]
        ev = [
            {"kind": "quote_selection", "segment_index": q.segment_index, "quote": q.quote}
            for q in ref.evidence
        ] or [{"kind": "no_evidence", "reason": "没有本项正向证据。"}]
        scores.append(
            {
                "criterion_id": str(doc.rubric_snapshot[idx].criterion_id),
                "awarded_points": str(ref.awarded_points),
                "rationale": ref.rationale,
                "evidence_references": ev,
            }
        )
    return json.dumps(
        {
            "schema_version": "1.0",
            "schema_name": "huipi.grading.quote-selection",
            "outcome": "candidate",
            "total_score": str(case.reference_total_score),
            "criterion_scores": scores,
        },
        ensure_ascii=False,
    )


class FakeProvider:
    provider_name = "openai_compatible"
    model_id = "deepseek-flash"
    generation_config_digest = "c" * 64

    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return TextGradingResponse(
            content=self.content,
            model_id=self.model_id,
            usage=ProviderUsage(prompt_tokens=10, completion_tokens=20),
            elapsed_ms=1,
            finish_reason="stop",
        )


def config():
    return Settings(
        _env_file=None,
        grading_max_output_tokens=4096,
        grading_timeout_seconds=120,
        grading_invalid_response_retries=0,
    )


def test_frozen_v1_and_new_v2_keep_schema_and_input_labels_separate():
    ds, _ = load_dataset(DATA / "rubric-cases-v4-semantics.json")
    c = ds.cases[0]
    doc = build_grade_input(c)
    a = experiment_prompts(c, "quote_selection", QUOTE_SELECTION_PROMPT_VERSION)
    b = experiment_prompts(c, "quote_selection", SEMANTICS_PROMPT_VERSION)
    assert a == (QUOTE_SELECTION_SYSTEM_PROMPT, build_quote_selection_prompt(doc))
    assert "全部必要条件" in b[0] and "部分分" in b[0] and "同义" in b[0]
    assert "不增加JSON字段" in b[0]
    pa, pb = map(json.loads, (a[1], b[1]))
    for key in (
        "question",
        "teacher_answer_key",
        "rubric",
        "student_answer_segments",
        "candidate_response_example",
        "abstention_response_example",
    ):
        assert pa[key] == pb[key]
    assert "reference_criterion_scores" not in pb and "reference_rationale" not in pb
    assert (
        unrequested_prediction(c, "quote_selection").prompt_sha256
        != unrequested_prediction(c, "quote_selection", SEMANTICS_PROMPT_VERSION).prompt_sha256
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case_id",
    [
        "v3-linear-arithmetic-slip",
        "v3-mixed-right-wrong",
        "v3-partial-quarter",
        "v3-english-keyword",
        "v3-fraction-equivalent",
        "v3-eq-no-work",
    ],
)
async def test_v2_uses_existing_decimal_score_and_quote_gates_with_fake_references(case_id):
    ds, digest = load_dataset(DATA / "rubric-semantics-regression-v1.json")
    case = next(c for c in ds.cases if c.case_id == case_id)
    provider = FakeProvider(reference_reply(case))
    p = await request_experiment_prediction(
        case, "quote_selection", provider, config(), prompt_version=SEMANTICS_PROMPT_VERSION
    )
    assert p.status == "candidate" and len(provider.calls) == 1
    cases = [
        p
        if c.case_id == case_id
        else unrequested_prediction(c, "quote_selection", SEMANTICS_PROMPT_VERSION)
        for c in ds.cases
    ]
    doc = create_experiment_predictions(
        ds,
        digest,
        "quote_selection",
        provider,
        config(),
        cases,
        prompt_version=SEMANTICS_PROMPT_VERSION,
    )
    report = evaluate_experiment(ds, digest, doc)
    assert report["total_score"]["mae"] == "0.0000"
    old = create_experiment_predictions(ds, digest, "quote_selection", provider, config(), cases)
    assert old.experiment_config_digest != doc.experiment_config_digest
    with pytest.raises(ValueError, match="prompt_sha256_mismatch"):
        evaluate_experiment(
            ds,
            digest,
            doc.model_copy(
                update={
                    "prompt_version": QUOTE_SELECTION_PROMPT_VERSION,
                    "experiment_config_digest": None,
                }
            ),
        )
    with pytest.raises(ValueError, match="dataset"):
        evaluate_experiment(ds, digest, doc.model_copy(update={"dataset_sha256": "a" * 64}))


@pytest.mark.anyio
async def test_v2_rejects_duplicate_quote_invalid_score_and_does_not_retry_timeout():
    ds, _ = load_dataset(DATA / "rubric-semantics-regression-v1.json")
    duplicate = next(c for c in ds.cases if c.case_id == "v3-quote-short-duplicate")
    pred = await request_experiment_prediction(
        duplicate,
        "quote_selection",
        FakeProvider(reference_reply(duplicate)),
        config(),
        prompt_version=SEMANTICS_PROMPT_VERSION,
    )
    assert pred.status == "invalid_evidence" and pred.error_code == "quote_ambiguous"
    c = ds.cases[0]
    raw = json.loads(reference_reply(c))
    raw["criterion_scores"][0]["awarded_points"] = "99.00"
    raw["total_score"] = "99.00"
    pred = await request_experiment_prediction(
        c,
        "quote_selection",
        FakeProvider(json.dumps(raw)),
        config(),
        prompt_version=SEMANTICS_PROMPT_VERSION,
    )
    assert pred.status == "invalid_rubric_scores"
    provider = FakeProvider(error=GradeProposalProviderTimeoutError())
    pred = await request_experiment_prediction(
        c, "quote_selection", provider, config(), prompt_version=SEMANTICS_PROMPT_VERSION
    )
    assert pred.status == "provider_timeout" and len(provider.calls) == 1


def test_original_v3_predictions_still_recompute_with_default_v1():
    from huipi_cloud.modules.grading.quote_evaluation import QuoteExperimentPredictions

    ds, digest = load_dataset(DATA / "rubric-cases-v3.json")
    path = DATA / "deepseek-flash-grading-v3-2026-10-14-run-01/quote_selection-predictions.json"
    pred = QuoteExperimentPredictions.model_validate_json(path.read_bytes())
    r = evaluate_experiment(ds, digest, pred)
    assert r["total_score"]["exact"] == {"numerator": 48, "denominator": 54}
    assert pred.prompt_version == QUOTE_SELECTION_PROMPT_VERSION


def test_unknown_prompt_and_changed_config_digest_are_rejected():
    ds, digest = load_dataset(DATA / "rubric-cases-v4-semantics.json")
    with pytest.raises(ValueError, match="unknown_quote"):
        experiment_prompts(ds.cases[0], "quote_selection", "not-a-version")
    pred = create_experiment_predictions(
        ds,
        digest,
        "quote_selection",
        FakeProvider(),
        config(),
        [unrequested_prediction(c, "quote_selection", SEMANTICS_PROMPT_VERSION) for c in ds.cases],
        prompt_version=SEMANTICS_PROMPT_VERSION,
    )
    with pytest.raises(ValueError, match="config_digest"):
        evaluate_experiment(
            ds, digest, pred.model_copy(update={"experiment_config_digest": "a" * 64})
        )
    with pytest.raises(ValueError, match="config_digest"):
        evaluate_experiment(ds, digest, pred.model_copy(update={"experiment_config_digest": None}))


def test_thinking_config_changes_provider_and_experiment_fingerprints_without_calls():
    from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider

    ds, digest = load_dataset(DATA / "rubric-cases-v4-semantics.json")
    base = Settings(
        _env_file=None,
        grading_enabled=True,
        grading_provider="openai_compatible",
        grading_base_url="https://api.deepseek.com",
        grading_model="deepseek-flash",
        grading_api_key="synthetic-test-only",
        grading_allow_remote=True,
        grading_external_data_authorized=True,
        grading_deepseek_thinking="disabled",
        grading_deepseek_reasoning_effort="max",
    )
    configs = [base, base.model_copy(update={"grading_deepseek_thinking": "enabled"})]
    documents = [
        create_experiment_predictions(
            ds,
            digest,
            "quote_selection",
            build_text_grading_provider(c),
            c,
            [
                unrequested_prediction(x, "quote_selection", SEMANTICS_PROMPT_VERSION)
                for x in ds.cases
            ],
            prompt_version=SEMANTICS_PROMPT_VERSION,
        )
        for c in configs
    ]
    assert documents[0].generation_config_digest != documents[1].generation_config_digest
    assert documents[0].experiment_config_digest != documents[1].experiment_config_digest
    assert documents[0].reasoning_effort is None
    assert documents[1].reasoning_effort == "max"
    assert all(not case.provider_attempted for d in documents for case in d.cases)
