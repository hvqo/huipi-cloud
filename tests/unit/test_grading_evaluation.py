from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import ProviderUsage, TextGradingResponse
from huipi_cloud.modules.grading.evaluation import (
    EvaluationPredictionCase,
    ReferenceEvidenceQuote,
    RubricEvaluationCase,
    RubricEvaluationDataset,
    RubricEvaluationPredictions,
    _diagnose_protocol_reply,
    build_grade_input,
    evaluate_predictions,
    load_dataset,
    request_prediction,
)

DATASET_PATH = Path(__file__).resolve().parents[2] / (
    "data/evaluation/grading/rubric-cases-v1.json"
)
V2_DATASET_PATH = DATASET_PATH.with_name("rubric-cases-v2.json")
V1_PREDICTIONS_PATH = DATASET_PATH.with_name("predictions-qwen3.5-4b-v1.json")
V1_REPORT_PATH = DATASET_PATH.with_name("report-qwen3.5-4b-v1.json")


def _metric_case(
    reference: tuple[str, str] = ("6.00", "4.00"),
) -> RubricEvaluationCase:
    score_a, score_b = (Decimal(value) for value in reference)
    return RubricEvaluationCase(
        case_id="metric-example",
        case_kind="metric_test",
        question_type="calculation",
        stem="按两项规则评价答案。",
        max_score=Decimal("10.00"),
        answer_key="alpha beta",
        rubric=[
            {"criterion_key": "first", "description": "检查alpha。", "max_points": "6.00"},
            {"criterion_key": "second", "description": "检查beta。", "max_points": "4.00"},
        ],
        student_segments=[{"text": "alpha beta", "page_index": 0}],
        reference_status="included",
        reference_total_score=score_a + score_b,
        reference_criterion_scores=[
            {
                "criterion_key": "first",
                "awarded_points": score_a,
                "rationale": "第一项合成标注。",
                "evidence": (
                    [{"segment_index": 0, "quote": "alpha"}] if score_a > 0 else []
                ),
            },
            {
                "criterion_key": "second",
                "awarded_points": score_b,
                "rationale": "第二项合成标注。",
                "evidence": (
                    [{"segment_index": 0, "quote": "beta"}] if score_b > 0 else []
                ),
            },
        ],
        reference_rationale="LABEL_ONLY_REFERENCE_EXPLANATION",
        label_provenance="synthetic_curated_reference",
        difficulty="easy",
    )


def _dataset(case: RubricEvaluationCase | None = None) -> tuple[RubricEvaluationDataset, str]:
    dataset = RubricEvaluationDataset(
        dataset_name="huipi-rubric-candidate-grading",
        dataset_version="test-1",
        source="synthetic_curated_reference",
        description="仅供单元测试的人工构造合成评测数据。",
        cases=[case or _metric_case()],
    )
    digest = hashlib.sha256(b"fixed-test-dataset-bytes").hexdigest()
    return dataset, digest


def _reply_content(
    case: RubricEvaluationCase,
    scores: tuple[str, str],
    *,
    mutation: str | None = None,
) -> str:
    document = build_grade_input(case)
    criterion_scores = []
    for index, (criterion, awarded) in enumerate(
        zip(document.rubric_snapshot, scores, strict=True)
    ):
        score = Decimal(awarded)
        reference = (
            {"kind": "quote", "segment_index": 0, "text_start": 0, "text_end": 5, "quote": "alpha"}
            if index == 0 and score > 0
            else {
                "kind": "quote",
                "segment_index": 0,
                "text_start": 6,
                "text_end": 10,
                "quote": "beta",
            }
            if score > 0
            else {"kind": "no_evidence", "reason": "没有可计分的相关内容。"}
        )
        criterion_scores.append(
            {
                "criterion_id": str(criterion.criterion_id),
                "awarded_points": awarded,
                "rationale": "按合成题目及评分细则给出此项分数。",
                "evidence_references": [reference],
            }
        )
    reply: dict[str, object] = {
        "schema_version": "1.0",
        "outcome": "candidate",
        "total_score": str(sum((Decimal(item) for item in scores), Decimal("0.00"))),
        "criterion_scores": criterion_scores,
    }
    if mutation == "invalid_quote":
        criterion_scores[0]["evidence_references"] = [
            {"kind": "quote", "segment_index": 0, "text_start": 0, "text_end": 5, "quote": "xxxxx"}
        ]
    if mutation == "duplicate_criterion":
        criterion_scores[1]["criterion_id"] = criterion_scores[0]["criterion_id"]
    return json.dumps(reply, ensure_ascii=False)


def _prediction_document(
    dataset: RubricEvaluationDataset,
    digest: str,
    entries: list[EvaluationPredictionCase],
) -> RubricEvaluationPredictions:
    return RubricEvaluationPredictions(
        dataset_version=dataset.dataset_version,
        dataset_sha256=digest,
        provider="ollama_native",
        model_id="unit-test-provider",
        prompt_version="rubric-candidate-v3",
        generation_config_digest="a" * 64,
        invalid_response_retries=0,
        timeout_seconds=20,
        created_at="2026-01-01T00:00:00Z",
        cases=entries,
    )


def _candidate_prediction(
    case: RubricEvaluationCase,
    scores: tuple[str, str],
    *,
    mutation: str | None = None,
    status: str = "candidate",
    error_code: str | None = None,
) -> EvaluationPredictionCase:
    return EvaluationPredictionCase(
        case_id=case.case_id,
        status=status,
        response_content=_reply_content(case, scores, mutation=mutation),
        error_code=error_code,
        prompt_tokens=100,
        completion_tokens=25,
        elapsed_ms=80,
    )


def _report(scores: tuple[str, str], reference: tuple[str, str] = ("6.00", "4.00")):
    case = _metric_case(reference)
    dataset, digest = _dataset(case)
    predictions = _prediction_document(
        dataset,
        digest,
        [_candidate_prediction(case, scores)],
    )
    return evaluate_predictions(dataset, digest, predictions)


def test_dataset_has_twenty_four_included_cases_and_two_uncertain_cases():
    dataset, digest = load_dataset(DATASET_PATH)
    assert digest == "e9a7959e2ad8ff479073b870c468b0155e7e1395662697f4b8c4324dbbb8e6b5"
    assert len(dataset.cases) == 26
    assert sum(case.reference_status == "included" for case in dataset.cases) == 24
    assert sum(case.reference_status == "excluded_uncertain" for case in dataset.cases) == 2
    assert {case.label_provenance for case in dataset.cases} == {"synthetic_curated_reference"}


def test_v1_dataset_original_prediction_and_report_bytes_remain_immutable():
    expected_hashes = {
        DATASET_PATH: "e9a7959e2ad8ff479073b870c468b0155e7e1395662697f4b8c4324dbbb8e6b5",
        V1_PREDICTIONS_PATH: "12792766ad918a61468b5797d70a3eca2335bee7b9b853574136dbd7cde2ccdd",
        V1_REPORT_PATH: "75cbb75dda8e33ca7f44ecf37a7b37b011dca20853ee4f875c98b6670b27c8cb",
    }
    for path, expected in expected_hashes.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected


def test_v2_reference_audit_splits_fraction_policy_and_clarifies_process_credit():
    dataset, digest = load_dataset(V2_DATASET_PATH)
    assert dataset.dataset_version == "2.0"
    assert len(dataset.cases) == 27
    assert sum(case.reference_status == "included" for case in dataset.cases) == 25
    by_id = {case.case_id: case for case in dataset.cases}
    assert by_id["equivalent-fraction-strict-format"].reference_total_score == Decimal("0.00")
    assert (
        by_id["equivalent-fraction-equivalent-form-allowed"].reference_total_score
        == Decimal("5.00")
    )
    assert by_id["linear-missing-step-v2"].reference_total_score == Decimal("3.00")
    assert by_id["correct-transform-wrong-result-v2"].reference_total_score == Decimal("0.00")
    manifest_path = V2_DATASET_PATH.with_name("rubric-cases-v2-run-manifest.json")
    manifest = json.loads(manifest_path.read_text())
    assert manifest["dataset_sha256"] == digest
    assert manifest["provider_requests"] == 0
    assert {item["status"] for item in manifest["cases"]} == {
        "not_requested",
        "excluded_uncertain",
    }
    assert all(
        item["status"] == "not_requested"
        for item in manifest["cases"]
        if item["case_id"] in {
            "linear-missing-step-v2",
            "correct-transform-wrong-result-v2",
            "equivalent-fraction-strict-format",
            "equivalent-fraction-equivalent-form-allowed",
        }
    )


def test_v1_predictions_cannot_be_relabelled_as_v2_predictions():
    dataset_v1, digest_v1 = load_dataset(DATASET_PATH)
    _, digest_v2 = load_dataset(V2_DATASET_PATH)
    predictions_v1 = RubricEvaluationPredictions.model_validate_json(
        V1_PREDICTIONS_PATH.read_bytes()
    )
    assert predictions_v1.dataset_version == dataset_v1.dataset_version
    with pytest.raises(ValueError, match="prediction_dataset_version_mismatch"):
        evaluate_predictions(
            dataset_v1.model_copy(update={"dataset_version": "2.0"}),
            digest_v2,
            predictions_v1,
        )


def test_protocol_diagnostic_counts_quote_length_errors_without_text():
    case = _metric_case()
    payload = json.loads(_reply_content(case, ("6.00", "4.00")))
    first = payload["criterion_scores"][0]["evidence_references"][0]

    first["text_end"] = 4
    length_diagnostic = _diagnose_protocol_reply(case, json.dumps(payload))
    assert length_diagnostic["category_occurrences"]["invalid_quote_length"] == 1


def test_diagnostic_distinguishes_fabricated_text_empty_quote_and_no_evidence_shape():
    case = _metric_case()
    payload = json.loads(_reply_content(case, ("6.00", "4.00")))
    reference = payload["criterion_scores"][0]["evidence_references"][0]

    reference["quote"] = ""
    reference["text_end"] = 0
    empty = _diagnose_protocol_reply(case, json.dumps(payload))
    assert empty["category_occurrences"]["empty_quote"] == 1

    reference.clear()
    reference.update({"kind": "no_evidence", "reason": "无证据。", "segment_index": 0})
    no_evidence = _diagnose_protocol_reply(case, json.dumps(payload))
    assert no_evidence["category_occurrences"]["invalid_no_evidence_shape"] == 1


@pytest.mark.parametrize(
    ("payload", "expected_category"),
    [
        ("not json", "invalid_json"),
        (json.dumps({"outcome": "candidate"}), "missing_required_field"),
        (
            json.dumps({"schema_version": "1.0", "outcome": "maybe"}),
            "invalid_outcome",
        ),
    ],
)
def test_diagnostic_categorizes_json_missing_fields_and_outcome(payload, expected_category):
    case = _metric_case()
    diagnostic = _diagnose_protocol_reply(case, payload)
    assert diagnostic["category_occurrences"][expected_category] >= 1


def test_diagnostic_counts_unexpected_fields_invalid_scores_and_criterion_shape():
    case = _metric_case()
    payload = json.loads(_reply_content(case, ("6.00", "4.00")))
    payload["unexpected"] = True
    payload["criterion_scores"][0]["awarded_points"] = "NaN"
    diagnostic = _diagnose_protocol_reply(case, json.dumps(payload))
    assert diagnostic["category_occurrences"]["unexpected_field"] == 1
    assert diagnostic["category_occurrences"]["invalid_score_value"] == 1

    payload["criterion_scores"] = {"not": "a list"}
    malformed = _diagnose_protocol_reply(case, json.dumps(payload))
    assert malformed["category_occurrences"]["invalid_criterion_structure"] >= 1


def test_multiple_protocol_errors_count_cases_and_occurrences_separately_and_keep_input_bytes(
    tmp_path,
):
    case = _metric_case()
    dataset, digest = _dataset(case)
    payload = json.loads(_reply_content(case, ("6.00", "4.00")))
    payload["criterion_scores"][0]["evidence_references"][0]["text_end"] = 4
    payload["criterion_scores"][1]["evidence_references"][0]["text_end"] = 9
    prediction = EvaluationPredictionCase(
        case_id=case.case_id,
        status="protocol_error",
        error_code="model_evidence_protocol_invalid",
        response_content=json.dumps(payload),
    )
    predictions = _prediction_document(dataset, digest, [prediction])
    path = tmp_path / "saved-prediction.json"
    path.write_bytes(predictions.model_dump_json().encode())
    original_bytes = path.read_bytes()
    report = evaluate_predictions(dataset, digest, predictions)
    diagnostic = report["protocol_failure_diagnostics"]
    assert diagnostic["case_count"] == 1
    assert diagnostic["diagnostic_categories"]["invalid_quote_length"] == {
        "case_count": 1,
        "occurrence_count": 2,
    }
    assert report["counts"]["valid_candidate_cases"] == 0
    assert report["counts"]["prediction_status_counts"] == {"protocol_error": 1}
    assert path.read_bytes() == original_bytes
    serialized_report = json.dumps(report, ensure_ascii=False)
    assert "alpha" not in serialized_report and "beta" not in serialized_report


def test_v1_audited_report_separates_pipeline_stages_and_keeps_semantic_risks():
    dataset, digest = load_dataset(DATASET_PATH)
    predictions = RubricEvaluationPredictions.model_validate_json(V1_PREDICTIONS_PATH.read_bytes())
    prediction_bytes = V1_PREDICTIONS_PATH.read_bytes()
    report = evaluate_predictions(dataset, digest, predictions)
    assert V1_PREDICTIONS_PATH.read_bytes() == prediction_bytes
    assert report["evaluation_stages"]["provider_response_received"] == {
        "numerator": 24,
        "denominator": 24,
    }
    assert report["evaluation_stages"]["json_parseable"] == {
        "numerator": 24,
        "denominator": 24,
    }
    assert report["evaluation_stages"]["strict_grade_model_reply_valid"]["numerator"] == 3
    assert report["evaluation_stages"]["final_usable_candidate"] == {
        "numerator": 2,
        "denominator": 24,
    }
    assert report["evaluation_stages"]["candidate_total_score_comparison"] == {
        "exact_numerator": 2,
        "denominator": 2,
        "mae": "0.0000",
        "semantic_accuracy_claim": False,
    }
    old_report = json.loads(V1_REPORT_PATH.read_text())
    for field in (
        "counts",
        "coverage",
        "runtime_usage",
        "total_score",
        "criterion_score",
        "evidence",
        "failure_cases",
        "per_case",
    ):
        assert report[field] == old_report[field]
    assert "ratio-simplification" not in {
        item["case_id"] for item in report["protocol_failure_diagnostics"]["cases"]
    }
    diagnostics = report["protocol_failure_diagnostics"]
    assert diagnostics["case_count"] == 21
    assert diagnostics["diagnostic_categories"]["invalid_quote_length"] == {
        "case_count": 19,
        "occurrence_count": 33,
    }
    assert diagnostics["diagnostic_categories"]["invalid_quote_offsets"] == {
        "case_count": 11,
        "occurrence_count": 21,
    }
    length_or_offset_case_ids = {
        item["case_id"]
        for item in diagnostics["cases"]
        if {"invalid_quote_length", "invalid_quote_offsets"} & set(item["categories"])
    }
    assert len(length_or_offset_case_ids) == 20
    assert diagnostics["diagnostic_categories"]["empty_quote"] == {
        "case_count": 2,
        "occurrence_count": 2,
    }
    assert diagnostics["diagnostic_categories"]["invalid_no_evidence_shape"] == {
        "case_count": 1,
        "occurrence_count": 1,
    }
    assert report["semantic_score_risk_review"]["case_count"] == 12
    assert report["source_quote_failure_diagnostics"]["diagnostic_categories"][
        "positive_score_without_quote"
    ] == {"case_count": 1, "occurrence_count": 1}
    assert next(
        case for case in dataset.cases if case.case_id == "equivalent-fraction"
    ).reference_total_score == Decimal("5.00")
    risks = report["semantic_score_risk_review"]
    risk_ids = {item["case_id"] for item in risks["cases"]}
    assert "linear-wrong-answer" in risk_ids
    assert "off-topic-zero" in risk_ids
    assert "zero-evidence-absence" in risk_ids
    assert all(not item["included_in_candidate_score_metrics"] for item in risks["cases"])
    assert risks["rationales_and_quotes_included"] is False


def test_valid_quotes_create_no_protocol_or_source_failure_diagnostic():
    case = _metric_case()
    dataset, digest = _dataset(case)
    prediction = _candidate_prediction(case, ("6.00", "4.00"))
    report = evaluate_predictions(
        dataset,
        digest,
        _prediction_document(dataset, digest, [prediction]),
    )
    assert report["protocol_failure_diagnostics"]["case_count"] == 0
    assert report["source_quote_failure_diagnostics"]["case_count"] == 0
    assert report["counts"]["valid_candidate_cases"] == 1


def test_other_protocol_error_is_a_safe_fallback_category():
    diagnostic = _diagnose_protocol_reply(_metric_case(), "[]")
    assert diagnostic["category_occurrences"]["other_protocol_error"] == 1


def test_protocol_diagnostics_do_not_raise_candidate_coverage_for_malformed_quote():
    case = _metric_case()
    dataset, digest = _dataset(case)
    payload = json.loads(_reply_content(case, ("6.00", "4.00")))
    payload["criterion_scores"][0]["evidence_references"][0]["quote"] = "gamma"
    prediction = EvaluationPredictionCase(
        case_id=case.case_id,
        status="invalid_evidence",
        error_code="evidence_quote_mismatch",
        response_content=json.dumps(payload),
    )
    report = evaluate_predictions(
        dataset,
        digest,
        _prediction_document(dataset, digest, [prediction]),
    )
    assert report["counts"]["valid_candidate_cases"] == 0
    assert report["total_score"]["mae"] is None
    assert report["source_quote_failure_diagnostics"]["diagnostic_categories"][
        "invalid_quote_offsets"
    ]["case_count"] == 0
    assert report["source_quote_failure_diagnostics"]["diagnostic_categories"][
        "invalid_quote_text"
    ] == {"case_count": 1, "occurrence_count": 1}


@pytest.mark.parametrize(("text_start", "text_end"), [(1, 6), (4, 9)])
def test_source_quote_diagnostics_locate_misoffset_text_without_accepting_candidate(
    text_start, text_end
):
    case = _metric_case()
    dataset, digest = _dataset(case)
    payload = json.loads(_reply_content(case, ("6.00", "4.00")))
    evidence = payload["criterion_scores"][0]["evidence_references"][0]
    evidence["text_start"], evidence["text_end"] = text_start, text_end
    prediction = EvaluationPredictionCase(
        case_id=case.case_id,
        status="invalid_evidence",
        error_code="evidence_quote_mismatch",
        response_content=json.dumps(payload),
    )
    report = evaluate_predictions(
        dataset,
        digest,
        _prediction_document(dataset, digest, [prediction]),
    )
    assert report["counts"]["valid_candidate_cases"] == 0
    diagnostic = report["source_quote_failure_diagnostics"]
    assert diagnostic["diagnostic_categories"]["invalid_quote_offsets"] == {
        "case_count": 1,
        "occurrence_count": 1,
    }
    quote_check = diagnostic["cases"][0]["quote_checks"][0]
    assert quote_check["source_exact_match_count"] == 1
    assert quote_check["matches_claimed_offsets"] is False


def test_perfect_scores_and_evidence_match_are_counted_exactly():
    report = _report(("6.00", "4.00"))
    assert report["total_score"]["exact"] == {"numerator": 1, "denominator": 1}
    assert report["total_score"]["within_tolerance"] == {"numerator": 1, "denominator": 1}
    assert report["criterion_score"]["exact"] == {"numerator": 2, "denominator": 2}
    assert report["total_score"]["mae"] == "0.0000"
    assert report["evidence"]["reference_quote_overlap_characters"] == {
        "gold": 9,
        "predicted": 9,
        "intersection": 9,
        "precision": "1.0000",
        "recall": "1.0000",
        "iou": "1.0000",
    }


def test_decimal_half_point_boundary_is_within_tolerance_but_not_exact():
    report = _report(("5.50", "4.00"))
    assert report["total_score"]["absolute_error_sum"] == "0.50"
    assert report["total_score"]["exact"] == {"numerator": 0, "denominator": 1}
    assert report["total_score"]["within_tolerance"] == {"numerator": 1, "denominator": 1}
    assert report["total_score"]["under_count"] == 1
    assert report["criterion_score"]["mae"] == "0.2500"


def test_over_and_under_scoring_counts_are_separate():
    over = _report(("5.00", "4.00"), reference=("4.00", "4.00"))
    under = _report(("3.00", "3.00"), reference=("4.00", "4.00"))
    assert over["total_score"]["over_count"] == 1
    assert over["total_score"]["under_count"] == 0
    assert under["total_score"]["over_count"] == 0
    assert under["total_score"]["under_count"] == 1


def test_offsetting_criterion_errors_do_not_disappear_in_exact_total():
    report = _report(("5.00", "3.00"), reference=("4.00", "4.00"))
    assert report["total_score"]["exact"] == {"numerator": 1, "denominator": 1}
    assert report["criterion_score"]["exact"] == {"numerator": 0, "denominator": 2}
    assert report["criterion_score"]["offsetting_error_cases"] == 1
    assert report["criterion_score"]["offsetting_error_case_ids"] == ["metric-example"]
    assert report["failure_cases"][0]["failure_categories"][-1] == "offsetting_criterion_errors"


@pytest.mark.parametrize(
    ("reference", "prediction", "expected_total", "expected_criterion"),
    [
        (("0.00", "0.00"), ("0.00", "0.00"), "0.0000", "0.0000"),
        (("3.50", "2.00"), ("3.50", "2.00"), "0.0000", "0.0000"),
    ],
)
def test_zero_and_partial_decimal_scores(
    reference, prediction, expected_total, expected_criterion
):
    report = _report(prediction, reference)
    assert report["total_score"]["mae"] == expected_total
    assert report["criterion_score"]["mae"] == expected_criterion


def test_abstention_is_reported_as_coverage_failure_not_zero_score():
    case = _metric_case()
    dataset, digest = _dataset(case)
    reply = {
        "schema_version": "1.0",
        "outcome": "abstain",
        "abstention_reason": "PRIVATE_SYNTHETIC_SOURCE_CONTENT，证据不足。",
        "reason_codes": ["insufficient_evidence"],
        "total_score": None,
        "criterion_scores": [],
    }
    predictions = _prediction_document(
        dataset,
        digest,
        [
            EvaluationPredictionCase(
                case_id=case.case_id,
                status="abstain",
                response_content=json.dumps(reply, ensure_ascii=False),
            )
        ],
    )
    report = evaluate_predictions(dataset, digest, predictions)
    assert report["counts"]["abstention_cases"] == 1
    assert report["counts"]["valid_candidate_cases"] == 0
    assert report["total_score"]["mae"] is None
    assert report["coverage"]["candidate_over_requested"] == "0.0000"
    assert report["failure_cases"][0]["failure_categories"] == [
        "model_abstained_on_scored_reference"
    ]
    assert "PRIVATE_SYNTHETIC_SOURCE_CONTENT" not in json.dumps(report, ensure_ascii=False)


def test_timeout_is_counted_and_never_converted_to_a_score():
    case = _metric_case()
    dataset, digest = _dataset(case)
    predictions = _prediction_document(
        dataset,
        digest,
        [
            EvaluationPredictionCase(
                case_id=case.case_id,
                status="provider_timeout",
                error_code="provider_timeout",
                elapsed_ms=20000,
            )
        ],
    )
    report = evaluate_predictions(dataset, digest, predictions)
    assert report["counts"]["prediction_status_counts"] == {"provider_timeout": 1}
    assert report["total_score"]["exact"] == {"numerator": 0, "denominator": 0}
    assert report["total_score"]["mae"] is None


def test_invalid_json_and_invalid_quote_are_not_candidate_coverage():
    case = _metric_case()
    dataset, digest = _dataset(case)
    invalid_json = EvaluationPredictionCase(
        case_id=case.case_id,
        status="protocol_error",
        response_content="not json",
        error_code="model_json_contract_invalid",
    )
    invalid_json_report = evaluate_predictions(
        dataset,
        digest,
        _prediction_document(dataset, digest, [invalid_json]),
    )
    assert invalid_json_report["counts"]["prediction_status_counts"] == {
        "protocol_error": 1
    }
    assert invalid_json_report["counts"]["protocol_valid_replies"] == 0

    invalid_quote = _candidate_prediction(
        case,
        ("6.00", "4.00"),
        mutation="invalid_quote",
        status="invalid_evidence",
        error_code="evidence_quote_mismatch",
    )
    report = evaluate_predictions(
        dataset,
        digest,
        _prediction_document(dataset, digest, [invalid_quote]),
    )
    assert report["counts"]["valid_candidate_cases"] == 0
    assert report["counts"]["prediction_status_counts"] == {"invalid_evidence": 1}
    assert report["counts"]["protocol_valid_replies"] == 1
    assert report["evidence"]["invalid_quote_references"] == 1
    assert report["total_score"]["mae"] is None


def test_duplicate_criterion_is_rejected_by_reused_p3b_validator():
    case = _metric_case()
    dataset, digest = _dataset(case)
    prediction = _candidate_prediction(
        case,
        ("6.00", "4.00"),
        mutation="duplicate_criterion",
        status="invalid_rubric_scores",
        error_code="rubric_criterion_unknown_or_duplicate",
    )
    report = evaluate_predictions(
        dataset,
        digest,
        _prediction_document(dataset, digest, [prediction]),
    )
    assert report["counts"]["prediction_status_counts"] == {
        "invalid_rubric_scores": 1
    }


def test_dataset_and_prediction_case_sets_and_hash_must_match():
    case = _metric_case()
    dataset, digest = _dataset(case)
    good = _candidate_prediction(case, ("6.00", "4.00"))
    with pytest.raises(ValueError, match="dataset_sha256_mismatch"):
        evaluate_predictions(
            dataset,
            digest,
            _prediction_document(dataset, "b" * 64, [good]),
        )
    second_case = case.model_copy(update={"case_id": "metric-example-2"})
    two_case_dataset = dataset.model_copy(update={"cases": [case, second_case]})
    with pytest.raises(ValueError, match="case_set_mismatch"):
        evaluate_predictions(
            two_case_dataset,
            digest,
            _prediction_document(dataset, digest, [good]),
        )
    with pytest.raises(ValueError, match="duplicate_prediction_case_id"):
        evaluate_predictions(dataset, digest, _prediction_document(dataset, digest, [good, good]))


def test_manifest_detects_changed_dataset_bytes(tmp_path):
    data = DATASET_PATH.read_bytes()
    path = tmp_path / "dataset.json"
    path.write_bytes(data)
    path.with_suffix(".json.sha256").write_text("0" * 64, encoding="ascii")
    with pytest.raises(ValueError, match="manifest"):
        load_dataset(path)


def test_uncertain_reference_cases_are_excluded_without_prediction_leakage():
    case = RubricEvaluationCase(
        case_id="uncertain-only",
        case_kind="ambiguous",
        question_type="short_answer",
        stem="解释含糊要求。",
        max_score=Decimal("5.00"),
        answer_key="无法确定",
        rubric=[
            {
                "criterion_key": "answer",
                "description": "没有可操作标准。",
                "max_points": "5.00",
            }
        ],
        student_segments=[{"text": "可能是这样。", "page_index": 0}],
        reference_status="excluded_uncertain",
        reference_exclusion_reason="没有足够明确的参考标准。",
        label_provenance="synthetic_curated_reference",
        difficulty="hard",
    )
    dataset, digest = _dataset(case)
    predictions = _prediction_document(
        dataset,
        digest,
        [EvaluationPredictionCase(case_id=case.case_id, status="excluded_uncertain")],
    )
    report = evaluate_predictions(dataset, digest, predictions)
    assert report["counts"]["included_reference_cases"] == 0
    assert report["counts"]["requested_cases"] == 0
    assert report["counts"]["excluded_uncertain_reference_cases"] == 1
    assert report["total_score"]["mae"] is None


def test_prediction_schema_cannot_contain_reference_labels():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EvaluationPredictionCase(
            case_id="x",
            status="candidate",
            response_content="{}",
            reference_total_score="10.00",
        )


def test_prompt_and_transient_grade_input_exclude_gold_rationales_and_labels():
    from huipi_cloud.modules.grading.prompts import build_user_prompt

    dataset, _ = load_dataset(DATASET_PATH)
    case = next(case for case in dataset.cases if case.case_id == "prompt-injection-data")
    document = build_grade_input(case)
    prompt = build_user_prompt(document)
    serialized_input = json.dumps(document.model_dump(mode="json"), ensure_ascii=False)
    assert "LABEL_ONLY_REFERENCE_EXPLANATION" not in prompt
    assert case.reference_rationale not in prompt
    assert "reference_criterion_scores" not in serialized_input
    assert "label_provenance" not in serialized_input
    assert case.student_segments[0].text in prompt


def test_offline_recalculation_is_deterministic_and_subset_denominators_are_explicit():
    case = _metric_case()
    dataset, digest = _dataset(case)
    predictions = _prediction_document(
        dataset,
        digest,
        [EvaluationPredictionCase(case_id=case.case_id, status="not_requested")],
    )
    first = evaluate_predictions(dataset, digest, predictions)
    second = evaluate_predictions(dataset, digest, predictions)
    assert first == second
    assert first["counts"]["included_reference_cases"] == 1
    assert first["counts"]["requested_cases"] == 0
    assert first["counts"]["not_requested_cases"] == 1
    assert first["coverage"]["candidate_over_included_dataset"] == "0.0000"


def test_prediction_cases_retain_runtime_usage():
    case = _metric_case()
    dataset, digest = _dataset(case)
    prediction = _candidate_prediction(case, ("6.00", "4.00"))
    report = evaluate_predictions(
        dataset,
        digest,
        _prediction_document(dataset, digest, [prediction]),
    )
    assert report["runtime_usage"] == {
        "elapsed_ms_total": 80,
        "elapsed_ms_count": 1,
        "elapsed_ms_mean": 80.0,
        "prompt_tokens_total": 100,
        "prompt_token_count": 1,
        "completion_tokens_total": 25,
        "completion_token_count": 1,
    }


@pytest.mark.anyio
async def test_request_prediction_reuses_provider_and_classifies_timeout():
    from huipi_cloud.modules.grading.proposal_errors import GradeProposalProviderTimeoutError

    class TimeoutProvider:
        provider_name = "ollama_native"
        model_id = "timeout-test"
        generation_config_digest = "c" * 64

        async def complete(self, *, system_prompt: str, user_prompt: str):
            raise GradeProposalProviderTimeoutError("provider_timeout")

    prediction = await request_prediction(
        _metric_case(),
        TimeoutProvider(),
        Settings(grading_invalid_response_retries=0),
    )
    assert prediction.status == "provider_timeout"
    assert prediction.error_code == "provider_timeout"
    assert prediction.response_content is None
    assert prediction.elapsed_ms is not None


@pytest.mark.anyio
async def test_request_prediction_marks_invalid_json_without_saving_a_candidate():
    class InvalidJsonProvider:
        provider_name = "ollama_native"
        model_id = "invalid-json-test"
        generation_config_digest = "d" * 64

        async def complete(self, *, system_prompt: str, user_prompt: str):
            return TextGradingResponse(
                content="not json",
                model_id=self.model_id,
                elapsed_ms=12,
                usage=ProviderUsage(prompt_tokens=10, completion_tokens=2),
            )

    prediction = await request_prediction(
        _metric_case(),
        InvalidJsonProvider(),
        Settings(grading_invalid_response_retries=0),
    )
    assert prediction.status == "protocol_error"
    assert prediction.error_code == "model_json_contract_invalid"
    assert prediction.response_content == "not json"


def test_dataset_rejects_duplicate_case_ids_and_bad_reference_quote():
    case = _metric_case()
    with pytest.raises(ValidationError, match="unique"):
        RubricEvaluationDataset(
            dataset_name="huipi-rubric-candidate-grading",
            dataset_version="dup",
            source="synthetic_curated_reference",
            description="duplicate test",
            cases=[case, case],
        )
    case_data = case.model_dump(mode="python")
    case_data["reference_criterion_scores"][0]["evidence"] = [
        ReferenceEvidenceQuote(segment_index=0, quote="not-present")
    ]
    with pytest.raises(ValidationError, match="quote"):
        RubricEvaluationCase.model_validate(case_data)
