#!/usr/bin/env python3
"""Recompute frozen prompt comparisons; no model calls and no business writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pydantic import ValidationError  # noqa: E402

from huipi_cloud.modules.grading.evaluation import build_grade_input, load_dataset  # noqa: E402
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError  # noqa: E402
from huipi_cloud.modules.grading.quote_evaluation import (  # noqa: E402
    ExperimentPredictionCase,
    QuoteExperimentPredictions,
    _derive_selection_prediction,
    evaluate_experiment,
    experiment_prompts,
)
from huipi_cloud.modules.grading.quote_selection import (  # noqa: E402
    GradeQuoteSelectionReply,
    QuoteSelectionReference,
    locate_quote,
)

DATA = ROOT / "data/evaluation/grading"
RUN = DATA / "rubric-semantics-2026-10-14-run-01"
V1 = "rubric-quote-selection-v1"
V2 = "rubric-quote-selection-v2"
KNOWN = {
    "v3-linear-arithmetic-slip",
    "v3-mixed-right-wrong",
    "v3-partial-quarter",
    "v3-english-keyword",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def check_sha(path):
    actual = sha(path)
    if path.with_suffix(path.suffix + ".sha256").read_text().split()[0] != actual:
        raise ValueError("frozen_sha_mismatch:" + path.name)
    return actual


def locked_dataset(path, manifest_path, criteria_path):
    check_sha(path)
    check_sha(manifest_path)
    criteria_sha = check_sha(criteria_path)
    ds, digest = load_dataset(path)
    manifest = read(manifest_path)
    if (manifest["dataset_version"], manifest["dataset_sha256"], manifest["case_count"]) != (
        ds.dataset_version,
        digest,
        len(ds.cases),
    ):
        raise ValueError("frozen_dataset_manifest_mismatch")
    if manifest["acceptance_criteria_sha256"] != criteria_sha or not all(
        [
            manifest["labels_locked_before_predictions"],
            manifest["acceptance_criteria_locked_before_model_requests"],
        ]
    ):
        raise ValueError("preregistration_mismatch")
    if (
        manifest.get("model_predictions_used_to_author_labels") is not False
        or manifest.get("teacher_double_scored") is not False
    ):
        raise ValueError("label_provenance_mismatch")
    return ds, digest


def outcomes(ds, pred, probes=()):
    by = {p.case_id: p for p in pred.cases}
    by.update({p.case_id: p for p in probes})
    config = pred.generation_options.config(pred.timeout_seconds)
    rows = []
    review = []
    for c in ds.cases:
        p = by[c.case_id]
        status, reply, code = _derive_selection_prediction(c, p, config)
        raw = None
        if p.response_content:
            try:
                raw = GradeQuoteSelectionReply.model_validate_json(p.response_content)
            except ValidationError:
                pass
        doc = build_grade_input(c)
        keys = {
            str(x.criterion_id): c.rubric[i].criterion_key
            for i, x in enumerate(doc.rubric_snapshot)
        }
        refs = {x.criterion_key: x.awarded_points for x in c.reference_criterion_scores}
        scores = {
            keys.get(str(x.criterion_id), "unknown"): x.awarded_points
            for x in (raw.criterion_scores if raw else [])
        }
        exact = (
            status == "candidate"
            and c.reference_status == "included"
            and reply.total_score == c.reference_total_score
            and scores == refs
        )
        total = str(reply.total_score) if reply and reply.outcome == "candidate" else None
        rows.append(
            {
                "case_id": c.case_id,
                "reference_status": c.reference_status,
                "status": status,
                "error_code": code,
                "reference_total": str(c.reference_total_score)
                if c.reference_total_score is not None
                else None,
                "model_total": total,
                "reference_criteria": {k: str(v) for k, v in refs.items()},
                "raw_model_criteria": {k: str(v) for k, v in scores.items()},
                "all_scores_exact": exact,
                "valid_positive_criteria": sum(v > 0 for v in scores.values())
                if status == "candidate"
                else 0,
                "human_review_status": "unreviewed",
            }
        )
        review.append(
            {
                "case_id": c.case_id,
                "question": c.stem,
                "answer_key": c.answer_key,
                "rubric": [x.model_dump(mode="json") for x in c.rubric],
                "student_segments": [x.model_dump(mode="json") for x in c.student_segments],
                "reference_scores": [
                    x.model_dump(mode="json") for x in c.reference_criterion_scores
                ],
                "model_scores": [x.model_dump(mode="json") for x in raw.criterion_scores]
                if raw
                else [],
                "status": status,
                "human_review_status": "unreviewed",
                "semantic_support_status": "needs_human_review",
                "human_review_notes": "",
            }
        )
    return rows, review


def group_metrics(ds, digest, pred, probes):
    report = evaluate_experiment(ds, digest, pred)
    rows, review = outcomes(ds, pred, probes)
    calls = [p for p in [*pred.cases, *probes] if p.provider_attempted]
    included = [r for r in rows if r["reference_status"] == "included"]
    accepted = {r["case_id"] for r in included if r["status"] == "candidate"}
    quote_n = quote_ok = accepted_n = accepted_ok = json_ok = protocol_ok = 0
    failures = []
    by = {c.case_id: c for c in ds.cases}
    config = pred.generation_options.config(pred.timeout_seconds)
    for p in calls:
        if not p.response_content:
            continue
        try:
            json.loads(p.response_content)
            json_ok += 1
        except json.JSONDecodeError:
            continue
        try:
            raw = GradeQuoteSelectionReply.model_validate_json(p.response_content)
            protocol_ok += 1
        except ValidationError:
            continue
        doc = build_grade_input(by[p.case_id])
        for c in raw.criterion_scores:
            for q in c.evidence_references:
                if not isinstance(q, QuoteSelectionReference):
                    continue
                quote_n += 1
                isaccepted = p.case_id in accepted
                accepted_n += int(isaccepted)
                try:
                    locate_quote(doc, q.segment_index, q.quote, config)
                except GradeProposalError as error:
                    failures.append({"case_id": p.case_id, "reason_code": error.reason_code})
                else:
                    quote_ok += 1
                    accepted_ok += int(isaccepted)
    usage = {
        field: {
            "total": sum(getattr(p, field) or 0 for p in calls),
            "reported_calls": sum(getattr(p, field) is not None for p in calls),
        }
        for field in ("prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens")
    }
    peak = (
        Decimal(usage["prompt_tokens"]["total"]) * Decimal(".30")
        + Decimal(usage["completion_tokens"]["total"]) * Decimal("1.20")
    ) / 1_000_000
    cache_complete = all(
        usage[f]["reported_calls"] == len(calls) for f in ("cache_hit_tokens", "cache_miss_tokens")
    )
    offpeak = (
        (
            Decimal(usage["cache_hit_tokens"]["total"]) * Decimal(".003")
            + Decimal(usage["cache_miss_tokens"]["total"]) * Decimal(".15")
            + Decimal(usage["completion_tokens"]["total"]) * Decimal(".60")
        )
        / 1_000_000
        if cache_complete
        else None
    )
    zeros = [r for r in included if Decimal(r["reference_total"]) == 0]
    positive_zero = [
        r["case_id"] for r in zeros if r["status"] == "candidate" and Decimal(r["model_total"]) > 0
    ]
    raw_false_credit = [
        {
            "case_id": r["case_id"],
            "criterion_key": k,
            "model_points": v,
            "candidate_accepted": r["status"] == "candidate",
        }
        for r in included
        for k, v in r["raw_model_criteria"].items()
        if k in r["reference_criteria"]
        and Decimal(r["reference_criteria"][k]) == 0
        and Decimal(v) > 0
    ]
    fully_correct = [r for r in included if Decimal(r["reference_total"]) == 10]
    deductions = [
        r["case_id"]
        for r in fully_correct
        if r["status"] == "candidate" and Decimal(r["model_total"]) < 10
    ]
    probes_rows = [r for r in rows if r["reference_status"] != "included"]
    return {
        "prompt_version": pred.prompt_version,
        "generation_config_digest": pred.generation_config_digest,
        "experiment_config_digest": pred.experiment_config_digest,
        "new_requests": len(calls),
        "provider_success": {
            "numerator": sum(p.response_model_id is not None for p in calls),
            "denominator": len(calls),
        },
        "json_valid": {"numerator": json_ok, "denominator": len(calls)},
        "protocol_valid": {"numerator": protocol_ok, "denominator": len(calls)},
        "candidate_coverage": {"numerator": len(accepted), "denominator": len(included)},
        "all_raw_quotes": {"numerator": quote_ok, "denominator": quote_n, "failures": failures},
        "accepted_candidate_quotes": {"numerator": accepted_ok, "denominator": accepted_n},
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "scored_case_abstentions": sum(r["status"] == "abstain" for r in included),
        "uncertain_probe_abstentions": {
            "numerator": sum(r["status"] == "abstain" for r in probes_rows),
            "denominator": len(probes_rows),
        },
        "total_score": report["total_score"],
        "criterion_score": report["criterion_score"],
        "zero_reference_positive_cases": positive_zero,
        "zero_reference_cases": len(zeros),
        "raw_zero_criterion_positive_intents": raw_false_credit,
        "fully_correct_reference_cases": len(fully_correct),
        "fully_correct_not_full_score_cases": deductions,
        "fully_correct_without_candidate_cases": [
            r["case_id"] for r in fully_correct if r["status"] != "candidate"
        ],
        "cost_estimate_complete": all(
            usage[field]["reported_calls"] == len(calls)
            for field in ("prompt_tokens", "completion_tokens")
        ),
        "semantic_review_pending_positive_criteria": sum(
            r["valid_positive_criteria"] for r in rows
        ),
        "usage": usage,
        "all_call_elapsed_ms": sum(p.elapsed_ms or 0 for p in calls),
        "calls_missing_elapsed": sum(p.elapsed_ms is None for p in calls),
        "peak_uncached_estimate_usd": str(peak),
        "offpeak_cache_estimate_usd": str(offpeak) if offpeak is not None else None,
        "response_model_id_counts": dict(Counter(p.response_model_id for p in calls)),
        "cases": rows,
    }, review


def load_run(ds, digest, path, version):
    pred = QuoteExperimentPredictions.model_validate_json(
        (path / "quote_selection-predictions.json").read_bytes()
    )
    if (
        pred.prompt_version != version
        or pred.thinking_mode != "disabled"
        or pred.max_output_tokens != 4096
        or pred.timeout_seconds != 120
    ):
        raise ValueError("run_configuration_mismatch")
    evaluate_experiment(ds, digest, pred)
    manifest = read(path / "run-manifest.json")
    if (
        manifest.get("dataset_sha256") != digest
        or manifest.get("prompt_version") != version
        or manifest.get("requested_call_count") != len(ds.cases)
        or manifest.get("invalid_response_retries") != 0
        or manifest.get("preregistered", {}).get("acceptance_criteria_sha256")
        != check_sha(DATA / "rubric-semantics-acceptance-v1.json")
    ):
        raise ValueError("run_manifest_mismatch")
    probes = []
    if any(c.reference_status != "included" for c in ds.cases):
        probe_doc = read(path / "abstention-probes.json")
        if (probe_doc["dataset_sha256"], probe_doc["prompt_version"]) != (digest, version):
            raise ValueError("probe_provenance_mismatch")
        probes = [ExperimentPredictionCase.model_validate(p) for p in probe_doc["cases"]]
        if len(probes) != len({p.case_id for p in probes}) or {p.case_id for p in probes} != {
            c.case_id for c in ds.cases if c.reference_status != "included"
        }:
            raise ValueError("probe_case_set_mismatch")
    return pred, probes


def pair(a, b):
    left = {r["case_id"]: r for r in a["cases"]}
    right = {r["case_id"]: r for r in b["cases"]}
    if set(left) != set(right):
        raise ValueError("paired_case_set_mismatch")
    return [
        {
            "case_id": id,
            "a": left[id],
            "b": right[id],
            "new_error": left[id]["all_scores_exact"] and not right[id]["all_scores_exact"],
            "recovered": not left[id]["all_scores_exact"] and right[id]["all_scores_exact"],
        }
        for id in left
    ]


def build_summary(run_dir=RUN):
    criteria_path = DATA / "rubric-semantics-acceptance-v1.json"
    criteria = read(criteria_path)
    thresholds = criteria["gates"]
    freeze = read(run_dir / "experiment-freeze.json")
    check_sha(run_dir / "experiment-freeze.json")
    if check_sha(criteria_path) != freeze["acceptance_criteria_sha256"]:
        raise ValueError("criteria_freeze_mismatch")
    reg, regsha = locked_dataset(
        DATA / "rubric-semantics-regression-v1.json",
        DATA / "rubric-semantics-regression-v1-manifest.json",
        criteria_path,
    )
    hold, holdsha = locked_dataset(
        DATA / "rubric-cases-v4-semantics.json",
        DATA / "rubric-cases-v4-semantics-manifest.json",
        criteria_path,
    )
    old, oldsha = load_dataset(DATA / "rubric-cases-v3.json")
    if oldsha != "de59c65d31669043ffd49c062318861a542bd039cfd04c7df4d4645dc4df620d":
        raise ValueError("old_v3_changed")
    old_by = {c.case_id: c for c in old.cases}
    if any(c != old_by[c.case_id] for c in reg.cases):
        raise ValueError("regression_inputs_or_labels_changed")
    historical = (
        DATA / "deepseek-flash-grading-v3-2026-10-14-run-01/quote_selection-predictions.json"
    )
    if sha(historical) != "9b141f5af5a962ec988476ab4722c14319b48264292a037dba9a0d163b24eedf":
        raise ValueError("old_predictions_changed")
    original = QuoteExperimentPredictions.model_validate_json(historical.read_bytes())
    evaluate_experiment(old, oldsha, original)
    selected = {c.case_id for c in reg.cases}
    history = original.model_copy(
        update={
            "dataset_version": reg.dataset_version,
            "dataset_sha256": regsha,
            "cases": [p for p in original.cases if p.case_id in selected],
        }
    )
    reg_b, reg_probes = load_run(reg, regsha, run_dir / "regression-v2", V2)
    hold_a, probe_a = load_run(hold, holdsha, run_dir / "holdout-v1", V1)
    hold_b, probe_b = load_run(hold, holdsha, run_dir / "holdout-v2", V2)
    for field in (
        "provider",
        "model_id",
        "generation_config_digest",
        "generation_options",
        "thinking_mode",
        "reasoning_effort",
        "max_output_tokens",
        "timeout_seconds",
    ):
        if len({str(getattr(p, field)) for p in (history, reg_b, hold_a, hold_b)}) != 1:
            raise ValueError("paired_model_or_config_mismatch:" + field)
    actual_preflight = sum(
        Decimal(
            read(run_dir / name / "run-manifest.json")["cost_preflight"]["preflight_estimate_usd"]
        )
        for name in ("regression-v2", "holdout-v1", "holdout-v2")
    )
    if actual_preflight != Decimal(freeze["aggregate_preflight_usd"]) or (
        actual_preflight > Decimal("0.50") or freeze["planned_new_requests"] != 64
    ):
        raise ValueError("aggregate_preflight_mismatch")
    for version in (V1, V2):
        system, _ = experiment_prompts(hold.cases[0], "quote_selection", version)
        if hashlib.sha256(system.encode()).hexdigest() != freeze["system_prompt_sha256"][version]:
            raise ValueError("frozen_prompt_changed")
    ra, rva = group_metrics(reg, regsha, history, [])
    rb, rvb = group_metrics(reg, regsha, reg_b, reg_probes)
    ha, hva = group_metrics(hold, holdsha, hold_a, probe_a)
    hb, hvb = group_metrics(hold, holdsha, hold_b, probe_b)
    ra["new_requests"] = 0
    ra["historical_requests_reused"] = 20
    pr = pair(ra, rb)
    ph = pair(ha, hb)
    control = [r for r in pr if r["case_id"] not in KNOWN | {"v3-quote-short-duplicate"}]
    known = [r for r in rb["cases"] if r["case_id"] in KNOWN]

    def rate(obj):
        return Decimal(obj["numerator"]) / obj["denominator"] if obj["denominator"] else Decimal(0)

    gates = {
        "known_four_exact": len(known) == 4 and all(r["all_scores_exact"] for r in known),
        "normal_controls_no_new_errors": sum(r["new_error"] for r in control)
        <= thresholds["normal_controls_new_errors_allowed"],
        "holdout_total_mae": Decimal(hb["total_score"]["mae"] or "Infinity")
        <= Decimal(thresholds["holdout_total_mae_max"]),
        "holdout_criterion_mae": Decimal(hb["criterion_score"]["mae"] or "Infinity")
        <= Decimal(thresholds["holdout_criterion_mae_max"]),
        "holdout_total_exact": rate(hb["total_score"]["exact"])
        >= Decimal(thresholds["holdout_total_exact_min"]),
        "holdout_criterion_exact": rate(hb["criterion_score"]["exact"])
        >= Decimal(thresholds["holdout_criterion_exact_min"]),
        "zero_score_not_overcredited": not rb["zero_reference_positive_cases"]
        and not hb["zero_reference_positive_cases"],
        "wrong_or_missing_step_not_overcredited": not [
            x
            for x in hb["raw_zero_criterion_positive_intents"]
            if x["candidate_accepted"] and x["case_id"] in criteria["critical_holdout_case_ids"]
        ],
        "uncertain_probes_abstain": hb["uncertain_probe_abstentions"]["numerator"]
        == thresholds["holdout_probe_abstain_required"],
        "qualified_human_semantic_review": False,
    }
    for name, group in [("regression_v2", rb), ("holdout_v2", hb)]:
        for metric, threshold in [
            ("provider_success", "provider_success_rate_min"),
            ("protocol_valid", "protocol_valid_rate_min"),
            ("candidate_coverage", "valid_candidate_coverage_min"),
            ("all_raw_quotes", "all_raw_quote_unique_location_rate_min"),
        ]:
            gates[name + "_" + metric] = rate(group[metric]) >= Decimal(thresholds[threshold])
    all_calls = [
        p
        for pred, probes in [(reg_b, reg_probes), (hold_a, probe_a), (hold_b, probe_b)]
        for p in [*pred.cases, *probes]
        if p.provider_attempted
    ]
    if len(all_calls) != 64 or len({p.request_id for p in all_calls}) != 64:
        raise ValueError("unexpected_or_duplicate_requests")
    summary = {
        "report_version": "1.0",
        "data_classification": "synthetic_only",
        "freeze_commit": "3dd11cd488e41880f289cac2b2a1842626493ac7",
        "dataset_sha256": {"regression": regsha, "holdout": holdsha},
        "criteria_sha256": sha(criteria_path),
        "historical_v3_prediction_sha256": sha(historical),
        "groups": {
            "regression_a_historical_v1": ra,
            "regression_b_v2": rb,
            "holdout_a_v1": ha,
            "holdout_b_v2": hb,
        },
        "regression_paired_cases": pr,
        "holdout_paired_cases": ph,
        "normal_control_degradations": [r["case_id"] for r in control if r["new_error"]],
        "preregistered_gate_results": gates,
        "numeric_gates_pass": all(
            v for k, v in gates.items() if k != "qualified_human_semantic_review"
        ),
        "production_integration_authorized": False,
        "new_provider_requests": len(all_calls),
        "all_human_reviews": "unreviewed",
        "semantic_support_automatically_proven": False,
        "limitations": [
            "v3 is a development regression set, not unseen holdout.",
            (
                "Historical A and current B are not simultaneous; single outputs and "
                "cloud model variability limit causal attribution."
            ),
            "Synthetic curated references are not qualified teacher double scores.",
            "Quote source equality does not prove semantic support.",
        ],
    }
    checklist = {
        "schema_version": "1.0",
        "synthetic_only": True,
        "human_review_status": "unreviewed",
        "formal_grade_review_records_written": False,
        "groups": {
            "regression_v1": rva,
            "regression_v2": rvb,
            "holdout_v1": hva,
            "holdout_v2": hvb,
        },
    }
    return summary, checklist


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=RUN)
    args = parser.parse_args()
    summary, checklist = build_summary(args.run_dir)
    for name, value in [
        ("semantics-summary.json", summary),
        ("semantic-human-review.json", checklist),
    ]:
        with (args.run_dir / name).open("x", encoding="utf-8") as f:
            f.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "new_requests": summary["new_provider_requests"],
                "gates": summary["preregistered_gate_results"],
                "normal_control_degradations": summary["normal_control_degradations"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
