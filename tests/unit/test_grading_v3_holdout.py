from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from huipi_cloud.core.config import Settings
from huipi_cloud.modules.grading.evaluation import build_grade_input, load_dataset
from huipi_cloud.modules.grading.quote_evaluation import ExperimentPredictionCase
from huipi_cloud.modules.grading.quote_selection import build_quote_selection_prompt
from scripts import evaluate_quote_selection as runner
from scripts.validate_grading_v3_holdout import validate_holdout

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "data/evaluation/grading/rubric-cases-v3.json"
MANIFEST = ROOT / "data/evaluation/grading/rubric-cases-v3-manifest.json"


def test_v3_holdout_has_locked_reference_labels_and_required_risk_coverage():
    report = validate_holdout()

    assert report["dataset_version"] == "3.0"
    assert report["dataset_sha256"] == (
        "de59c65d31669043ffd49c062318861a542bd039cfd04c7df4d4645dc4df620d"
    )
    assert report["case_count"] == 60
    assert report["scored_cases"] == 55
    assert report["excluded_uncertain_abstention_probes"] == 5
    assert report["labels_locked_before_predictions"] is True
    assert report["teacher_double_scored"] is False
    assert report["provider_requests_sent"] == 0


def test_v3_reference_labels_are_not_sent_to_quote_selection_prompt():
    dataset, _ = load_dataset(DATASET)
    case = next(item for item in dataset.cases if item.reference_status == "included")
    prompt = build_quote_selection_prompt(build_grade_input(case))
    payload = json.loads(prompt)

    assert "reference_total_score" not in payload
    assert "reference_criterion_scores" not in payload
    assert "reference_rationale" not in payload
    assert case.reference_rationale not in prompt


def test_standalone_run_keeps_uncertain_abstention_probe_out_of_score_file(
    tmp_path, monkeypatch
):
    source_dataset, digest = load_dataset(DATASET)
    cases = [
        next(item for item in source_dataset.cases if item.reference_status == "included"),
        next(
            item
            for item in source_dataset.cases
            if item.reference_status == "excluded_uncertain"
        ),
    ]
    dataset = source_dataset.model_copy(update={"cases": cases})
    config = Settings(
        _env_file=None,
        grading_provider="openai_compatible",
        grading_base_url="https://api.deepseek.com",
        grading_model="deepseek-flash",
        grading_deepseek_thinking="disabled",
        grading_invalid_response_retries=0,
        grading_allow_remote=True,
        grading_external_data_authorized=True,
        grading_api_key="synthetic-test-key",
        grading_max_output_tokens=1024,
        grading_timeout_seconds=30,
    )
    provider = SimpleNamespace(
        provider_name="openai_compatible",
        model_id="deepseek-flash",
        generation_config_digest="a" * 64,
    )
    calls: list[str] = []

    monkeypatch.setattr(runner, "load_dataset", lambda _: (dataset, digest))
    monkeypatch.setattr(runner, "Settings", lambda: config)
    monkeypatch.setattr(runner, "build_text_grading_provider", lambda _: provider)
    monkeypatch.setattr(
        runner,
        "_cost_preflight",
        lambda requested, _config, _args: {
            "request_cap": len(requested),
            "input_usd_per_million": "0.30",
            "output_usd_per_million": "1.20",
            "budget_usd": "0.50",
            "definition": "测试预检。",
        },
    )

    async def request(case, mode, _provider, _config):
        calls.append(case.case_id)
        base = runner.unrequested_prediction(case, mode).model_dump()
        base.update(
            {
                "status": "candidate" if case.reference_status == "included" else "abstain",
                "response_content": '{"outcome":"abstain"}',
                "request_id": uuid4(),
                "provider_attempted": True,
                "response_model_id": "deepseek-flash",
                "prompt_tokens": 120,
                "completion_tokens": 15,
                "elapsed_ms": 40,
                "finish_reason": "stop",
            }
        )
        return ExperimentPredictionCase.model_validate(base)

    monkeypatch.setattr(runner, "request_experiment_prediction", request)
    monkeypatch.setattr(
        runner,
        "create_experiment_predictions",
        lambda _dataset, _digest, _mode, _provider, _config, predictions: SimpleNamespace(
            cases=predictions,
            model_dump=lambda mode="json": {
                "cases": [item.model_dump(mode=mode) for item in predictions]
            },
        ),
    )

    def evaluate(_dataset, _digest, predictions):
        return {
            "counts": {
                "provider_requests_sent": sum(
                    item.provider_attempted for item in predictions.cases
                )
            },
            "total_score": {},
            "criterion_score": {},
            "runtime_usage": {},
        }

    monkeypatch.setattr(runner, "evaluate_experiment", evaluate)
    args = runner._parser().parse_args(
        [
            "--run-model",
            "--standalone",
            "--probe-excluded-uncertain",
            "--dataset",
            str(DATASET),
            "--preregistered-manifest",
            str(MANIFEST),
            "--output-dir",
            str(tmp_path / "new-run"),
            "--max-requests",
            "1",
        ]
    )

    result = asyncio.run(runner._run_model(args))

    assert calls == [case.case_id for case in cases]
    assert result["groups"].keys() == {"quote_selection"}
    out = tmp_path / "new-run"
    prediction_file = json.loads((out / "quote_selection-predictions.json").read_text())
    assert prediction_file["cases"][0]["status"] == "candidate"
    assert prediction_file["cases"][0]["provider_attempted"] is True
    assert prediction_file["cases"][1]["status"] == "excluded_uncertain"
    assert prediction_file["cases"][1]["provider_attempted"] is False
    probe_file = json.loads((out / "abstention-probes.json").read_text())
    assert probe_file["score_metrics_included"] is False
    assert probe_file["cases"][0]["status"] == "abstain"
    probe_report = json.loads((out / "abstention-probe-report.json").read_text())
    assert probe_report["abstain_cases"] == 1
    assert probe_report["probe_cases"] == 1
    assert probe_report["cases"][0]["human_review_status"] == "unreviewed"
    run_manifest = json.loads((out / "run-manifest.json").read_text())
    assert run_manifest["preregistered"]["acceptance_criteria_sha256"] == (
        "8d4de2bdf2691ad2e494e343af22702665f2aface31eb521e4492700b3a5c861"
    )
