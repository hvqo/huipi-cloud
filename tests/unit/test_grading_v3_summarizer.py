from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from huipi_cloud.core.config import Settings
from huipi_cloud.modules.grading.evaluation import (
    build_grade_input,
    load_dataset,
)
from huipi_cloud.modules.grading.quote_evaluation import (
    ExperimentPredictionCase,
    create_experiment_predictions,
    evaluate_experiment,
    unrequested_prediction,
)
from scripts.summarize_grading_v3 import build_report, write_versioned_reports

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "data/evaluation/grading/rubric-cases-v3.json"
CRITERIA = ROOT / "data/evaluation/grading/p3d2a-acceptance-criteria-v1.json"


def _candidate_reply(case) -> str:
    document = build_grade_input(case)
    criterion_ids = {
        criterion.criterion_key: str(document.rubric_snapshot[index].criterion_id)
        for index, criterion in enumerate(case.rubric)
    }
    reference_scores = {
        score.criterion_key: score for score in case.reference_criterion_scores
    }
    criterion_scores = []
    for criterion in case.rubric:
        reference = reference_scores[criterion.criterion_key]
        evidence = []
        if reference.awarded_points > 0:
            for quote in reference.evidence:
                # The whole synthetic segment is a unique, exact quote for this offline fixture.
                evidence.append(
                    {
                        "kind": "quote_selection",
                        "segment_index": quote.segment_index,
                        "quote": (
                            quote.quote
                            if case.case_id == "v3-quote-short-duplicate"
                            else case.student_segments[quote.segment_index].text
                        ),
                    }
                )
        else:
            evidence.append(
                {"kind": "no_evidence", "reason": "测试夹具中没有此项的正向证据。"}
            )
        criterion_scores.append(
            {
                "criterion_id": criterion_ids[criterion.criterion_key],
                "awarded_points": str(reference.awarded_points),
                "rationale": reference.rationale,
                "evidence_references": evidence,
            }
        )
    return json.dumps(
        {
            "schema_version": "1.0",
            "schema_name": "huipi.grading.quote-selection",
            "outcome": "candidate",
            "total_score": str(case.reference_total_score),
            "criterion_scores": criterion_scores,
        },
        ensure_ascii=False,
    )


def _probe_reply() -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "schema_name": "huipi.grading.quote-selection",
            "outcome": "abstain",
            "abstention_reason": "测试夹具模拟材料不足时弃权。",
            "criterion_scores": [],
        },
        ensure_ascii=False,
    )


def _prediction(
    case, response: str, *, status: str, error_code: str | None = None
) -> ExperimentPredictionCase:
    base = unrequested_prediction(case, "quote_selection").model_dump(mode="json")
    base.update(
        {
            "status": status,
            "error_code": error_code,
            "response_content": response,
            "request_id": str(uuid4()),
            "provider_attempted": True,
            "response_model_id": "deepseek-flash",
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "elapsed_ms": 50,
            "finish_reason": "stop",
        }
    )
    return ExperimentPredictionCase.model_validate(base)


def test_summarizer_recomputes_scores_and_keeps_all_human_reviews_unreviewed(tmp_path):
    dataset, digest = load_dataset(DATASET)
    settings = Settings(
        _env_file=None,
        grading_max_output_tokens=4096,
        grading_timeout_seconds=120,
    )
    provider = SimpleNamespace(
        provider_name="openai_compatible",
        model_id="deepseek-flash",
        generation_config_digest="b" * 64,
    )
    scored_cases = [
        _prediction(
            case,
            _candidate_reply(case),
            status=(
                "invalid_evidence"
                if case.case_id == "v3-quote-short-duplicate"
                else "candidate"
            ),
            error_code=(
                "quote_ambiguous" if case.case_id == "v3-quote-short-duplicate" else None
            ),
        )
        if case.reference_status == "included"
        else unrequested_prediction(case, "quote_selection")
        for case in dataset.cases
    ]
    predictions = create_experiment_predictions(
        dataset, digest, "quote_selection", provider, settings, scored_cases
    )
    report = evaluate_experiment(dataset, digest, predictions)
    run_dir = tmp_path / "offline-fixture-run"
    run_dir.mkdir()
    (run_dir / "quote_selection-predictions.json").write_text(
        predictions.model_dump_json(indent=2), encoding="utf-8"
    )
    (run_dir / "quote_selection-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "run-manifest.json").write_text(
        json.dumps(
            {
                "dataset_sha256": digest,
                "requested_call_count": 60,
                "invalid_response_retries": 0,
                "preregistered": {
                    "acceptance_criteria_sha256": hashlib.sha256(
                        CRITERIA.read_bytes()
                    ).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )
    probe_cases = [
        _prediction(
            case,
            _probe_reply(),
            status="abstain",
        )
        for case in dataset.cases
        if case.reference_status == "excluded_uncertain"
    ]
    (run_dir / "abstention-probes.json").write_text(
        json.dumps(
            {
                "dataset_sha256": digest,
                "score_metrics_included": False,
                "cases": [case.model_dump(mode="json") for case in probe_cases],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary, checklist = build_report(DATASET, run_dir, CRITERIA)

    assert summary["dataset"]["scored_cases"] == 55
    assert summary["engineering_validity"]["excluded_uncertain_probes"][
        "expected_abstain"
    ] == 5
    assert summary["score_quality"]["total_score"]["mae"] == "0.0000"
    assert summary["score_quality"]["critical_zero_reference_criterion_false_credit"] == []
    assert summary["human_review"]["status"] == "unreviewed"
    assert summary["report_version"] == "1.1"
    quote_metrics = summary["engineering_validity"]["quote_location"]
    assert quote_metrics["all_raw_model_quote_references"]["exact_location_rate"][
        "numerator"
    ] == 93
    assert quote_metrics["all_raw_model_quote_references"]["exact_location_rate"][
        "denominator"
    ] == 94
    assert quote_metrics["all_raw_model_quote_references"]["failure_reason_counts"] == {
        "quote_ambiguous": 1
    }
    assert quote_metrics["accepted_candidate_quotes"]["exact_location_rate"] == {
        "numerator": 92,
        "denominator": 92,
        "rate": "1",
    }
    assert quote_metrics["accepted_candidate_quotes"]["location_failures"] == []
    assert summary["pre_registered_gates"]["results"][
        "accepted_quote_unique_exact_location"
    ] is True
    assert checklist["review_status_default"] == "unreviewed"
    assert checklist["report_version"] == "1.1"
    assert len(checklist["cases"]) == 60
    assert summary["pre_registered_gates"]["results"]["human_semantic_gate_complete"] is False
    assert (
        summary["pre_registered_gates"]["eligible_for_later_production_integration"] is False
    )


def test_versioned_summary_does_not_overwrite_historical_report(tmp_path):
    historical_summary = tmp_path / "quality-summary.json"
    historical_checklist = tmp_path / "human-semantic-review.json"
    historical_summary.write_bytes(b"frozen-summary\n")
    historical_checklist.write_bytes(b"frozen-checklist\n")
    original_summary = historical_summary.read_bytes()
    original_checklist = historical_checklist.read_bytes()
    summary = {"report_version": "1.1", "result": "post-hoc"}
    checklist = {"report_version": "1.1", "review_status_default": "unreviewed"}

    summary_path, checklist_path = write_versioned_reports(tmp_path, summary, checklist)

    assert summary_path.name == "quality-summary-v1.1.json"
    assert checklist_path.name == "human-semantic-review-v1.1.json"
    assert historical_summary.read_bytes() == original_summary
    assert historical_checklist.read_bytes() == original_checklist
    assert summary_path.with_suffix(".json.sha256").exists()
    assert checklist_path.with_suffix(".json.sha256").exists()
    with pytest.raises(FileExistsError):
        write_versioned_reports(tmp_path, summary, checklist)
