from __future__ import annotations

import hashlib
import json

from scripts.audit_grading_v3 import build_audit_artifacts


def test_v3_audit_preserves_frozen_inputs_and_marks_disputed_labels():
    audit, regression = build_audit_artifacts()

    assert audit["source_dataset_sha256"] == (
        "de59c65d31669043ffd49c062318861a542bd039cfd04c7df4d4645dc4df620d"
    )
    assert audit["source_preregistered_criteria_sha256"] == (
        "8d4de2bdf2691ad2e494e343af22702665f2aface31eb521e4492700b3a5c861"
    )
    assert audit["case_count"] == 60
    assert audit["case_classification_counts"] == {
        "excluded_uncertain_material_or_rubric_issue": 5,
        "no_specific_issue_found_in_technical_screen": 53,
        "reference_label_dispute": 1,
        "source_incomplete": 1,
    }
    by_id = {item["case_id"]: item for item in audit["case_audits"]}
    assert by_id["v3-stem-copy-number"]["audit_classification"] == "source_incomplete"
    assert by_id["v3-stem-copy-number"]["included_in_posthoc_sensitivity"] is False
    assert by_id["v3-econ-opportunity-cost"]["audit_classification"] == "reference_label_dispute"
    assert by_id["v3-econ-opportunity-cost"]["human_review_status"] == "unreviewed"
    assert all(item["human_review_status"] == "unreviewed" for item in audit["case_audits"])
    assert audit["original_v3_inputs_labels_predictions_and_preregistration_modified"] is False

    sensitivity = audit["posthoc_sensitivity_analysis"]
    assert sensitivity["analysis_type"] == "post_hoc_sensitivity_analysis"
    assert sensitivity["model_calls"] == 0
    assert sensitivity["accepted_candidate_cases"] == 52
    assert sensitivity["total_score"]["exact_match"] == {"numerator": 48, "denominator": 52}
    assert sensitivity["total_score"]["absolute_error_sum"] == "15.00"
    assert sensitivity["total_score"]["overestimate_cases"] == 4
    assert sensitivity["total_score"]["underestimate_cases"] == 0
    assert sensitivity["criterion_score"]["exact_match"] == {
        "numerator": 109,
        "denominator": 113,
    }
    assert sensitivity["criterion_score"]["absolute_error_sum"] == "15.00"
    assert sensitivity["high_risk_zero_reference"]["accepted_positive_score_case_ids"] == [
        "v3-english-keyword"
    ]

    regression_by_id = {item["case_id"]: item for item in regression["records"]}
    assert regression["model_requests_made"] == 0
    assert len(regression_by_id) == 8
    assert regression_by_id["v3-linear-arithmetic-slip"]["categories"] == [
        "incorrect_arithmetic_credit",
        "incorrect_method_credit",
    ]
    assert regression_by_id["v3-stem-copy-number"]["reference_label_status"] == (
        "disputed_unreviewed"
    )
    assert regression_by_id["v3-econ-opportunity-cost"]["reference_label_status"] == (
        "disputed_unreviewed"
    )
    duplicate_quote = regression_by_id["v3-quote-short-duplicate"]
    assert duplicate_quote["prediction_error_code"] == "quote_ambiguous"
    assert len(duplicate_quote["raw_quote_references"]) == 2
    assert any(
        item["location_status"] == "quote_ambiguous"
        for item in duplicate_quote["raw_quote_references"]
    )
    missing_image = regression_by_id["v3-uncertain-missing-image"]
    assert missing_image["model_total_points"] == "0.00"
    assert missing_image["prediction_status"] == "candidate"
    assert all(item["human_review_status"] == "unreviewed" for item in regression["records"])


def test_audit_revalidates_raw_sha_without_mutating_historical_report():
    from scripts.audit_grading_v3 import DATA_DIR, DEFAULT_RUN

    frozen_files = [
        DATA_DIR / "rubric-cases-v3.json",
        DATA_DIR / "p3d2a-acceptance-criteria-v1.json",
        DEFAULT_RUN / "quote_selection-predictions.json",
        DEFAULT_RUN / "quote_selection-report.json",
        DEFAULT_RUN / "quality-summary.json",
    ]
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in frozen_files}

    build_audit_artifacts()

    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in frozen_files}
    assert after == before
    assert before[DEFAULT_RUN / "quality-summary.json"] == (
        "2adeef06f62f793ed6f7124018b75077bb7d004b4f00bd7dc07683b0c9b2e90d"
    )


def test_audit_output_schema_is_json_serializable_and_does_not_promote_reviews():
    audit, regression = build_audit_artifacts()
    encoded_audit = json.dumps(audit, ensure_ascii=False)
    encoded_regression = json.dumps(regression, ensure_ascii=False)

    assert '"human_review_status": "unreviewed"' in encoded_audit
    assert '"human_review_status": "unreviewed"' in encoded_regression
    assert "teacher_double_scored\": false" in encoded_audit
