#!/usr/bin/env python3
"""Validate the locked, synthetic-only P3-D2A v3 holdout before model calls."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from huipi_cloud.modules.grading.evaluation import (  # noqa: E402
    load_dataset,
)

DATA_DIR = PROJECT_ROOT / "data/evaluation/grading"
DATASET_PATH = DATA_DIR / "rubric-cases-v3.json"
MANIFEST_PATH = DATA_DIR / "rubric-cases-v3-manifest.json"
CRITERIA_PATH = DATA_DIR / "p3d2a-acceptance-criteria-v1.json"

REQUIRED_RISK_CATEGORIES = {
    "correct_final_only",
    "correct_intermediate_wrong_final",
    "lucky_correct_final",
    "partial_credit",
    "dependent_criteria",
    "equivalent_value_wrong_form",
    "chinese_paraphrase",
    "unbalanced_equation",
    "wrong_unit",
    "irrelevant_content",
    "prompt_content_confusion",
    "keyword_only",
    "mixed_correct_wrong",
    "repeated_quote",
    "duplicate_quote",
    "cross_page",
    "material_defect",
    "prompt_injection",
    "rubric_boundary",
    "overcredit_trap",
    "false_credit_guard",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_sidecar(path: Path) -> str:
    digest = _sha256(path)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    tokens = sidecar.read_text(encoding="ascii").strip().split()
    if not tokens or tokens[0] != digest:
        raise ValueError(f"sha256_sidecar_mismatch:{path.name}")
    return digest


def validate_holdout(
    dataset_path: Path = DATASET_PATH,
    manifest_path: Path = MANIFEST_PATH,
    criteria_path: Path = CRITERIA_PATH,
) -> dict[str, object]:
    dataset, dataset_sha = load_dataset(dataset_path)
    manifest_sha = _check_sidecar(manifest_path)
    manifest = json.loads(manifest_path.read_bytes())
    criteria_sha = _check_sidecar(criteria_path)
    criteria = json.loads(criteria_path.read_bytes())

    if manifest.get("dataset_sha256") != dataset_sha:
        raise ValueError("manifest_dataset_sha_mismatch")
    if manifest.get("acceptance_criteria_sha256") != criteria_sha:
        raise ValueError("manifest_acceptance_criteria_sha_mismatch")
    if not manifest.get("labels_locked_before_predictions"):
        raise ValueError("labels_not_locked_before_predictions")
    if manifest.get("predictions_exist_at_manifest_creation") is not False:
        raise ValueError("prediction_timing_not_attested")
    if manifest.get("model_predictions_used_to_author_labels") is not False:
        raise ValueError("model_predictions_used_for_reference_labels")
    if manifest.get("teacher_double_scored") is not False:
        raise ValueError("synthetic_labels_misrepresented_as_teacher_double_scored")
    if criteria.get("locked_before_model_requests") is not True:
        raise ValueError("acceptance_criteria_not_preregistered")
    if criteria.get("dataset_version") != dataset.dataset_version:
        raise ValueError("acceptance_criteria_dataset_version_mismatch")

    included = [case for case in dataset.cases if case.reference_status == "included"]
    excluded = [
        case for case in dataset.cases if case.reference_status == "excluded_uncertain"
    ]
    if (len(dataset.cases), len(included), len(excluded)) != (60, 55, 5):
        raise ValueError("v3_case_counts_mismatch")
    if dataset.source != "synthetic_curated_reference":
        raise ValueError("v3_reference_provenance_mismatch")

    case_entries = manifest.get("cases")
    if not isinstance(case_entries, list):
        raise ValueError("manifest_cases_missing")
    entries_by_id = {entry.get("case_id"): entry for entry in case_entries}
    if len(entries_by_id) != len(case_entries) or set(entries_by_id) != {
        case.case_id for case in dataset.cases
    }:
        raise ValueError("manifest_case_set_mismatch")
    found_risks = {
        tag
        for entry in case_entries
        for tag in entry.get("risk_categories", [])
    }
    missing_risks = REQUIRED_RISK_CATEGORIES - found_risks
    if missing_risks:
        raise ValueError("required_risk_coverage_missing:" + ",".join(sorted(missing_risks)))

    positives = 0
    for case in included:
        score_by_key = {item.criterion_key: item for item in case.reference_criterion_scores}
        rubric_by_key = {item.criterion_key: item for item in case.rubric}
        for criterion_key, reference in score_by_key.items():
            if reference.awarded_points <= 0:
                continue
            positives += 1
            if not reference.evidence:
                raise ValueError(f"positive_reference_without_quote:{case.case_id}")
            for quote in reference.evidence:
                source = case.student_segments[quote.segment_index].text
                start = quote.text_start
                if source[start : start + len(quote.quote)] != quote.quote:
                    raise ValueError(f"reference_quote_source_mismatch:{case.case_id}")
                if not rubric_by_key[criterion_key].description.strip():
                    raise ValueError(f"empty_rubric_criterion:{case.case_id}")
    if positives == 0:
        raise ValueError("no_positive_reference_evidence")

    for case in excluded:
        if case.reference_total_score is not None or case.reference_criterion_scores:
            raise ValueError(f"excluded_uncertain_has_score:{case.case_id}")
        entry = entries_by_id[case.case_id]
        if entry.get("expected_probe_outcome") != "abstain":
            raise ValueError(f"excluded_case_missing_abstention_probe:{case.case_id}")

    zero_ids = {
        case.case_id
        for case in included
        if case.reference_total_score == 0
    }
    if zero_ids != set(criteria.get("high_risk_zero_score_case_ids", [])):
        raise ValueError("high_risk_zero_case_set_mismatch")

    return {
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": dataset_sha,
        "manifest_sha256": manifest_sha,
        "acceptance_criteria_sha256": criteria_sha,
        "case_count": len(dataset.cases),
        "scored_cases": len(included),
        "excluded_uncertain_abstention_probes": len(excluded),
        "positive_reference_criterion_count": positives,
        "required_risk_categories": len(REQUIRED_RISK_CATEGORIES),
        "labels_locked_before_predictions": True,
        "teacher_double_scored": False,
        "provider_requests_sent": 0,
    }


def main() -> int:
    try:
        print(json.dumps(validate_holdout(), ensure_ascii=False, indent=2, sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"v3数据集校验失败：{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
