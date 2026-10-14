#!/usr/bin/env python3
"""Build a deterministic P3-D2A score report and synthetic-only review checklist."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pydantic import ValidationError  # noqa: E402

from huipi_cloud.modules.grading.evaluation import build_grade_input, load_dataset  # noqa: E402
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError  # noqa: E402
from huipi_cloud.modules.grading.quote_evaluation import (  # noqa: E402
    ExperimentPredictionCase,
    QuoteExperimentPredictions,
    _derive_selection_prediction,
    evaluate_experiment,
)
from huipi_cloud.modules.grading.quote_selection import (  # noqa: E402
    GradeQuoteSelectionReply,
    QuoteSelectionReference,
    locate_quote,
)

DATA_DIR = PROJECT_ROOT / "data/evaluation/grading"
DEFAULT_DATASET = DATA_DIR / "rubric-cases-v3.json"
DEFAULT_CRITERIA = DATA_DIR / "p3d2a-acceptance-criteria-v1.json"


def _json(path: Path):
    return json.loads(path.read_bytes())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rate(numerator: int, denominator: int) -> dict[str, object]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": str(Decimal(numerator) / Decimal(denominator)) if denominator else None,
    }


def _write_new(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_new_text(path: Path, value: str) -> None:
    with path.open("x", encoding="ascii") as stream:
        stream.write(value)


def write_versioned_reports(
    run_dir: Path, summary: dict[str, object], checklist: dict[str, object]
) -> tuple[Path, Path]:
    if summary.get("report_version") != "1.1":
        raise ValueError("unexpected_report_version")
    summary_path = run_dir / "quality-summary-v1.1.json"
    checklist_path = run_dir / "human-semantic-review-v1.1.json"
    paths = (
        summary_path,
        checklist_path,
        summary_path.with_suffix(summary_path.suffix + ".sha256"),
        checklist_path.with_suffix(checklist_path.suffix + ".sha256"),
    )
    if any(path.exists() for path in paths):
        raise FileExistsError("versioned_report_already_exists")
    _write_new(summary_path, summary)
    _write_new(checklist_path, checklist)
    _write_new_text(
        summary_path.with_suffix(summary_path.suffix + ".sha256"),
        f"{_sha(summary_path)}  {summary_path.name}\n",
    )
    _write_new_text(
        checklist_path.with_suffix(checklist_path.suffix + ".sha256"),
        f"{_sha(checklist_path)}  {checklist_path.name}\n",
    )
    return summary_path, checklist_path


def _nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


def _quote_location_metrics(
    located: int, selected: int, failures: list[dict[str, str]]
) -> dict[str, object]:
    return {
        "selected_quote_references": selected,
        "unique_exactly_located": located,
        "location_failures": failures,
        "failure_reason_counts": dict(Counter(item["reason_code"] for item in failures)),
        "exact_location_rate": _rate(located, selected),
    }


def build_report(
    dataset_path: Path,
    run_dir: Path,
    criteria_path: Path = DEFAULT_CRITERIA,
) -> tuple[dict[str, object], dict[str, object]]:
    dataset, dataset_sha = load_dataset(dataset_path)
    criteria = _json(criteria_path)
    predictions_path = run_dir / "quote_selection-predictions.json"
    report_path = run_dir / "quote_selection-report.json"
    run_manifest_path = run_dir / "run-manifest.json"
    probes_path = run_dir / "abstention-probes.json"
    predictions = QuoteExperimentPredictions.model_validate_json(predictions_path.read_bytes())
    stored_report = _json(report_path)
    run_manifest = _json(run_manifest_path)
    probes_doc = _json(probes_path)

    preregistered = run_manifest.get("preregistered") or {}
    if predictions.mode != "quote_selection":
        raise ValueError("v3_requires_quote_selection_predictions")
    if (predictions.dataset_version, predictions.dataset_sha256) != (
        dataset.dataset_version,
        dataset_sha,
    ):
        raise ValueError("v3_prediction_dataset_mismatch")
    if run_manifest.get("dataset_sha256") != dataset_sha:
        raise ValueError("v3_run_manifest_dataset_mismatch")
    if preregistered.get("acceptance_criteria_sha256") != _sha(criteria_path):
        raise ValueError("v3_run_did_not_bind_preregistered_criteria")
    if run_manifest.get("requested_call_count") != 60:
        raise ValueError("v3_run_request_count_mismatch")
    if run_manifest.get("invalid_response_retries") != 0:
        raise ValueError("v3_run_must_not_retry")
    if probes_doc.get("dataset_sha256") != dataset_sha or probes_doc.get(
        "score_metrics_included"
    ) is not False:
        raise ValueError("abstention_probe_metadata_mismatch")

    # Recompute the score report from the immutable raw replies before summarizing it.
    fresh_report = evaluate_experiment(dataset, dataset_sha, predictions)
    for key in (
        "counts",
        "total_score",
        "criterion_score",
        "evaluation_stages",
        "quote_location",
        "semantic_score_risk_review",
        "zero_reference_positive_score_risks",
    ):
        if stored_report.get(key) != fresh_report.get(key):
            raise ValueError(f"stored_report_revalidation_mismatch:{key}")

    by_case = {case.case_id: case for case in dataset.cases}
    prediction_by_id = {item.case_id: item for item in predictions.cases}
    if set(prediction_by_id) != set(by_case):
        raise ValueError("v3_prediction_case_set_mismatch")
    probe_records = [
        ExperimentPredictionCase.model_validate(item) for item in probes_doc.get("cases", [])
    ]
    excluded = {
        case.case_id: case
        for case in dataset.cases
        if case.reference_status == "excluded_uncertain"
    }
    if {item.case_id for item in probe_records} != set(excluded):
        raise ValueError("v3_abstention_probe_case_set_mismatch")
    for item in probe_records:
        if not item.provider_attempted or item.response_model_id != predictions.model_id:
            raise ValueError("v3_abstention_probe_request_metadata_mismatch")

    config = predictions.generation_options.config(predictions.timeout_seconds)
    all_calls = [item for item in [*predictions.cases, *probe_records] if item.provider_attempted]
    if len(all_calls) != 60:
        raise ValueError("v3_actual_request_count_mismatch")

    probe_by_id = {item.case_id: item for item in probe_records}
    derived_by_id = {}
    for case in dataset.cases:
        active = probe_by_id.get(case.case_id, prediction_by_id[case.case_id])
        derived_status, derived_error, reply = active.status, active.error_code, None
        if active.response_content is not None:
            derived_status, reply, derived_error = _derive_selection_prediction(
                case, active, config
            )
        derived_by_id[case.case_id] = (derived_status, derived_error, reply)

    provider_success = sum(item.response_model_id is not None for item in all_calls)
    response_received = sum(item.response_content is not None for item in all_calls)
    json_parseable = 0
    protocol_valid = 0
    quote_refs = 0
    located_refs = 0
    location_failures = []
    accepted_quote_refs = 0
    accepted_located_refs = 0
    accepted_location_failures = []
    outcome_counts: Counter[str] = Counter()
    probe_outcomes: dict[str, str] = {}

    for prediction in all_calls:
        case = by_case[prediction.case_id]
        if prediction.response_content is None:
            continue
        try:
            json.loads(prediction.response_content)
        except json.JSONDecodeError:
            continue
        json_parseable += 1
        try:
            parsed = GradeQuoteSelectionReply.model_validate_json(prediction.response_content)
        except ValidationError:
            continue
        protocol_valid += 1
        outcome_counts[parsed.outcome] += 1
        if case.case_id in excluded:
            probe_outcomes[case.case_id] = parsed.outcome
        document = build_grade_input(case)
        for score in parsed.criterion_scores:
            for reference in score.evidence_references:
                if not isinstance(reference, QuoteSelectionReference):
                    continue
                quote_refs += 1
                is_accepted_candidate = (
                    case.reference_status == "included"
                    and derived_by_id[case.case_id][0] == "candidate"
                )
                if is_accepted_candidate:
                    accepted_quote_refs += 1
                try:
                    locate_quote(document, reference.segment_index, reference.quote, config)
                except GradeProposalError as error:
                    failure = {"case_id": case.case_id, "reason_code": error.reason_code}
                    location_failures.append(failure)
                    if is_accepted_candidate:
                        accepted_location_failures.append(failure)
                else:
                    located_refs += 1
                    if is_accepted_candidate:
                        accepted_located_refs += 1

    included = [case for case in dataset.cases if case.reference_status == "included"]
    report_manifest = _json(DATA_DIR / "rubric-cases-v3-manifest.json")
    risk_by_id = {item["case_id"]: item["risk_categories"] for item in report_manifest["cases"]}
    critical_tags = set(criteria["critical_false_credit_tags"])
    false_total_positive = []
    critical_false_credit = []
    missing_step_false_credit = []
    checklist_cases = []

    for case in dataset.cases:
        prediction = prediction_by_id[case.case_id]
        derived_status, derived_error, reply = derived_by_id[case.case_id]

        document = build_grade_input(case)
        key_by_id = {
            str(item.criterion_id): case.rubric[index].criterion_key
            for index, item in enumerate(document.rubric_snapshot)
        }
        predicted_scores: dict[str, Decimal] = {}
        model_criteria = []
        if reply is not None and reply.outcome == "candidate":
            for score in reply.criterion_scores:
                key = key_by_id.get(str(score.criterion_id))
                if key is None:
                    continue
                predicted_scores[key] = score.awarded_points
                evidence = []
                for item in score.evidence_references:
                    if getattr(item, "kind", None) == "quote":
                        segment = case.student_segments[item.segment_index]
                        evidence.append(
                            {
                                "kind": "quote",
                                "segment_index": item.segment_index,
                                "page_index": segment.page_index,
                                "text_start": item.text_start,
                                "text_end": item.text_end,
                                "quote": item.quote,
                                "source_segment_text": segment.text,
                                "semantic_support_status": "needs_human_review",
                            }
                        )
                    else:
                        evidence.append(
                            {"kind": "no_evidence", "reason": item.reason}
                        )
                model_criteria.append(
                    {
                        "criterion_key": key,
                        "awarded_points": str(score.awarded_points),
                        "rationale": score.rationale,
                        "evidence_references": evidence,
                    }
                )

        reference_by_key = {
            item.criterion_key: item for item in case.reference_criterion_scores
        }
        score_differences = []
        for key, reference in reference_by_key.items():
            model_points = predicted_scores.get(key)
            difference = (
                model_points - reference.awarded_points if model_points is not None else None
            )
            score_differences.append(
                {
                    "criterion_key": key,
                    "reference_points": str(reference.awarded_points),
                    "model_points": str(model_points) if model_points is not None else None,
                    "difference": str(difference) if difference is not None else None,
                }
            )
            if model_points is None or reference.awarded_points != 0 or model_points <= 0:
                continue
            false_credit = {
                "case_id": case.case_id,
                "criterion_key": key,
                "model_points": str(model_points),
                "candidate_was_accepted": derived_status == "candidate",
            }
            if set(risk_by_id.get(case.case_id, [])) & critical_tags:
                critical_false_credit.append(false_credit)
            if "missing_steps" in risk_by_id.get(case.case_id, []):
                missing_step_false_credit.append(false_credit)

        predicted_total = (
            reply.total_score
            if reply is not None and reply.outcome == "candidate"
            else None
        )
        if (
            case.reference_status == "included"
            and case.reference_total_score == 0
            and predicted_total is not None
            and predicted_total > 0
        ):
            false_total_positive.append(
                {
                    "case_id": case.case_id,
                    "model_total_score": str(predicted_total),
                    "candidate_was_accepted": derived_status == "candidate",
                }
            )
        total_delta = (
            predicted_total - case.reference_total_score
            if predicted_total is not None and case.reference_total_score is not None
            else None
        )
        checklist_cases.append(
            {
                "case_id": case.case_id,
                "question": case.stem,
                "answer_key": case.answer_key,
                "rubric": [item.model_dump(mode="json") for item in case.rubric],
                "student_segments": [
                    item.model_dump(mode="json") for item in case.student_segments
                ],
                "reference_status": case.reference_status,
                "reference_total_score": (
                    str(case.reference_total_score)
                    if case.reference_total_score is not None
                    else None
                ),
                "reference_criterion_scores": [
                    item.model_dump(mode="json") for item in case.reference_criterion_scores
                ],
                "model_status": derived_status,
                "model_error_code": derived_error,
                "model_total_score": str(predicted_total) if predicted_total is not None else None,
                "model_criterion_scores": model_criteria,
                "criterion_score_differences": score_differences,
                "total_score_difference": str(total_delta) if total_delta is not None else None,
                "risk_categories": risk_by_id.get(case.case_id, []),
                "semantic_support_status": "needs_human_review",
                "human_review_status": "unreviewed",
                "human_review_notes": "",
            }
        )

    total_score = fresh_report["total_score"]
    criterion_score = fresh_report["criterion_score"]
    cancellation_ids = criterion_score["offsetting_error_case_ids"]
    criterion_mae = (
        Decimal(criterion_score["absolute_error_sum"])
        / Decimal(criterion_score["sample_count"])
        if criterion_score["sample_count"]
        else None
    )
    probe_abstentions = sum(value == "abstain" for value in probe_outcomes.values())
    latencies = [item.elapsed_ms for item in all_calls if item.elapsed_ms is not None]
    prompt_usage = [item.prompt_tokens for item in all_calls if item.prompt_tokens is not None]
    completion_usage = [
        item.completion_tokens for item in all_calls if item.completion_tokens is not None
    ]
    reasoning_usage = [
        item.reasoning_tokens for item in all_calls if item.reasoning_tokens is not None
    ]
    cache_hit_usage = [
        item.cache_hit_tokens for item in all_calls if item.cache_hit_tokens is not None
    ]
    cache_miss_usage = [
        item.cache_miss_tokens for item in all_calls if item.cache_miss_tokens is not None
    ]
    score_count = len(included)
    candidate_count = fresh_report["counts"]["valid_candidate_cases"]

    gates = {
        "provider_success_minimum": provider_success >= 59,
        "quote_selection_protocol_valid_minimum": protocol_valid >= 57,
        "candidate_coverage_minimum": candidate_count >= 44,
        "accepted_quote_unique_exact_location": (
            accepted_quote_refs > 0 and accepted_located_refs == accepted_quote_refs
        ),
        "abstention_probe_expected_abstain": probe_abstentions == len(excluded),
        "candidate_total_score_mae_maximum_points": (
            total_score["mae"] is not None and Decimal(total_score["mae"]) <= Decimal("0.75")
        ),
        "candidate_criterion_score_mae_maximum_points": (
            criterion_mae is not None and criterion_mae <= Decimal("0.50")
        ),
        "candidate_total_exact_match_minimum": (
            total_score["exact"]["denominator"] > 0
            and Decimal(total_score["exact"]["numerator"])
            / Decimal(total_score["exact"]["denominator"])
            >= Decimal("0.80")
        ),
        "candidate_criterion_exact_match_minimum": (
            criterion_score["exact"]["denominator"] > 0
            and Decimal(criterion_score["exact"]["numerator"])
            / Decimal(criterion_score["exact"]["denominator"])
            >= Decimal("0.80")
        ),
        "zero_reference_positive_total_score_allowed": len(false_total_positive) == 0,
        "critical_zero_reference_criterion_false_credit_allowed": len(critical_false_credit) == 0,
        "human_semantic_gate_complete": False,
    }
    summary = {
        "report_version": "1.1",
        "metric_definitions": {
            "quote_location": {
                "all_raw_model_quote_references": (
                    "所有通过Quote Selection协议的模型Quote；保留被拒绝候选中的定位失败。"
                ),
                "accepted_candidate_quotes": (
                    "仅统计计分案例中通过完整确定性候选校验的Quote；用于按v1文字重算原准入门槛。"
                ),
                "pre_registered_gate_binding": "accepted_candidate_quotes",
                "limitation": (
                    "候选Quote定位通过是候选接受的结构条件，不代表引用在教学语义上充分。"
                ),
            }
        },
        "dataset": {
            "version": dataset.dataset_version,
            "sha256": dataset_sha,
            "total_cases": len(dataset.cases),
            "scored_cases": score_count,
            "excluded_uncertain_probe_cases": len(excluded),
            "reference_source": dataset.source,
            "teacher_double_scored": False,
        },
        "engineering_validity": {
            "calls_attempted": len(all_calls),
            "response_model_id_counts": dict(
                Counter(item.response_model_id for item in all_calls if item.response_model_id)
            ),
            "thinking_mode": predictions.thinking_mode,
            "configured_reasoning_effort": predictions.configured_reasoning_effort,
            "effective_reasoning_effort": predictions.reasoning_effort,
            "generation_config_digest": predictions.generation_config_digest,
            "provider_response_success": _rate(provider_success, len(all_calls)),
            "response_content_received": _rate(response_received, len(all_calls)),
            "json_parseable": _rate(json_parseable, len(all_calls)),
            "quote_selection_protocol_valid": _rate(protocol_valid, len(all_calls)),
            "valid_candidate_coverage": _rate(candidate_count, score_count),
            "abstention_on_scored_cases": _rate(
                fresh_report["counts"]["abstention_cases"], score_count
            ),
            "status_counts_scored_cases": fresh_report["counts"]["prediction_status_counts"],
            "status_counts_all_calls": dict(Counter(item.status for item in all_calls)),
            "quote_location": {
                # Retain legacy keys as aliases for all raw model references.
                **_quote_location_metrics(located_refs, quote_refs, location_failures),
                "all_raw_model_quote_references": _quote_location_metrics(
                    located_refs, quote_refs, location_failures
                ),
                "accepted_candidate_quotes": _quote_location_metrics(
                    accepted_located_refs,
                    accepted_quote_refs,
                    accepted_location_failures,
                ),
                "scope_note": (
                    "有效候选Quote的唯一定位接近确定性验证器的必然结果，不能替代对所有原始Quote的监控。"
                ),
            },
            "excluded_uncertain_probes": {
                "probe_cases": len(excluded),
                "expected_abstain": probe_abstentions,
                "pass_rate": _rate(probe_abstentions, len(excluded)),
                "score_metrics_included": False,
            },
            "latency_ms": {
                "count": len(latencies),
                "p50_nearest_rank": _nearest_rank(latencies, 0.50),
                "p95_nearest_rank": _nearest_rank(latencies, 0.95),
                "mean": (
                    str(Decimal(sum(latencies)) / Decimal(len(latencies))) if latencies else None
                ),
                "total": sum(latencies),
            },
            "token_usage_all_calls": {
                "prompt_tokens_total": sum(prompt_usage),
                "prompt_token_count": len(prompt_usage),
                "completion_tokens_total": sum(completion_usage),
                "completion_token_count": len(completion_usage),
                "calls_missing_prompt_usage": len(all_calls) - len(prompt_usage),
                "calls_missing_completion_usage": len(all_calls) - len(completion_usage),
                "reasoning_tokens_total": sum(reasoning_usage),
                "reasoning_token_count": len(reasoning_usage),
                "cache_hit_tokens_total": sum(cache_hit_usage),
                "cache_hit_token_count": len(cache_hit_usage),
                "cache_miss_tokens_total": sum(cache_miss_usage),
                "cache_miss_token_count": len(cache_miss_usage),
                "usage_note": "reasoning_tokens是completion_tokens中的子集，不重复计费。",
            },
            "cost_estimate": stored_report.get("cost_estimate"),
            "failure_cases": fresh_report["failure_cases"],
        },
        "score_quality": {
            "total_score": total_score,
            "criterion_score": criterion_score,
            "offsetting_criterion_error_cases": len(cancellation_ids),
            "offsetting_criterion_error_case_ids": cancellation_ids,
            "zero_reference_positive_total_score_intents": false_total_positive,
            "critical_zero_reference_criterion_false_credit": critical_false_credit,
            "missing_step_false_credit": missing_step_false_credit,
            "semantic_quote_support": "not_evaluated; requires qualified human review",
        },
        "pre_registered_gates": {
            "criteria_version": criteria["criteria_version"],
            "registered_thresholds": {
                **criteria["engineering_gates"],
                **criteria["score_quality_gates"],
            },
            "results": gates,
            "passed_count": sum(gates.values()),
            "total_count": len(gates),
            "eligible_for_later_production_integration": all(gates.values()),
            "interpretation": (
                "即使所有合成数字门槛通过，人工语义复核和真实作业双评仍是独立必要条件。"
            ),
        },
        "human_review": {
            "checklist_file": "human-semantic-review-v1.1.json",
            "case_count": len(checklist_cases),
            "status": "unreviewed",
            "semantic_judgments_automated": False,
            "formal_grade_review_records_written": False,
        },
        "provenance": {
            "prediction_sha256": _sha(predictions_path),
            "report_sha256": _sha(report_path),
            "abstention_probe_sha256": _sha(probes_path),
            "run_manifest_sha256": _sha(run_manifest_path),
            "criteria_sha256": _sha(criteria_path),
            "synthetic_data_only": True,
        },
    }
    checklist = {
        "schema_name": "huipi.grading.synthetic-human-semantic-review",
        "schema_version": "1.1",
        "report_version": "1.1",
        "data_classification": "synthetic_only",
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": dataset_sha,
        "model_id": predictions.model_id,
        "prompt_version": predictions.prompt_version,
        "review_status_default": "unreviewed",
        "notes": "只供人工判断证据与评分语义；不写入GradeReviewRevision。",
        "cases": checklist_cases,
    }
    return summary, checklist


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--criteria", type=Path, default=DEFAULT_CRITERIA)
    args = parser.parse_args()
    try:
        summary, checklist = build_report(args.dataset, args.run_dir, args.criteria)
        summary_path, checklist_path = write_versioned_reports(
            args.run_dir, summary, checklist
        )
        print(
            json.dumps(
                {
                    "summary": str(summary_path),
                    "checklist": str(checklist_path),
                    "dataset_sha256": summary["dataset"]["sha256"],
                    "eligible_for_later_production_integration": summary[
                        "pre_registered_gates"
                    ]["eligible_for_later_production_integration"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as error:
        print(f"P3-D2A报告生成失败：{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
