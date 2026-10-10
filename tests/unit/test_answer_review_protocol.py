"""Protocol, annotation, and offline-metric validation for answer review."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.answer_review.annotations import AnswerPresenceAnnotationDataset
from huipi_cloud.modules.answer_review.evaluation import evaluate_dataset, evaluate_predictions
from huipi_cloud.modules.answer_review.protocol import AnswerReviewDecisionCreate
from huipi_cloud.modules.answer_review.service import _safe_asset_references
from huipi_cloud.modules.canonical_documents.protocol import CanonicalAssetReference
from huipi_cloud.workers import review_answers as review_cli

DATASET_PATH = Path(__file__).resolve().parents[1] / "fixtures/answer_presence_synthetic_v1.json"


def _dataset() -> AnswerPresenceAnnotationDataset:
    return AnswerPresenceAnnotationDataset.model_validate_json(DATASET_PATH.read_text())


def _region(pointer: str, start: int | None = None, end: int | None = None) -> dict:
    return {"content_pointer": pointer, "text_start": start, "text_end": end}


def test_presence_decisions_are_distinct_from_unreviewed() -> None:
    dataset = _dataset()
    absent = next(case for case in dataset.cases if case.decision == "response_absent")
    unreviewed = next(case for case in dataset.cases if case.review_status == "unreviewed")

    assert absent.decision == "response_absent"
    assert unreviewed.decision is None
    assert unreviewed.review_status == "unreviewed"


def test_absent_requires_a_cited_prompt_region() -> None:
    with pytest.raises(ValidationError):
        AnswerReviewDecisionCreate(
            decision="response_absent",
            reason_codes=["printed_prompt_only"],
            reviewer_ref="local-reviewer",
        )


def test_cli_converts_unexpected_failures_to_safe_error_code(monkeypatch, capsys) -> None:
    async def fail_with_sensitive_context(*_args):
        raise RuntimeError("student text and storage secret")

    async def no_op_dispose() -> None:
        return None

    monkeypatch.setattr(
        "sys.argv",
        ["review_answers.py", "inspect", "--submission-id", str(uuid4())],
    )
    monkeypatch.setattr(review_cli, "_run", fail_with_sensitive_context)
    monkeypatch.setattr(review_cli, "dispose_database_engine", no_op_dispose)

    assert asyncio.run(review_cli._main()) == 1

    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "failed", "failure_code": "internal_error"}
    assert "student text" not in output
    assert "storage secret" not in output
    assert "Traceback" not in output


def test_uncertain_can_be_explicit_without_claiming_absence() -> None:
    item = AnswerReviewDecisionCreate(
        decision="uncertain",
        uncertain_regions=[_region("/pages/0/blocks/0/content", 0, 2)],
        reason_codes=["ambiguous_handwriting"],
        reviewer_ref="local-reviewer",
    )
    assert item.decision == "uncertain"
    assert item.response_regions == []


def test_external_asset_uri_is_not_copied_to_review_evidence() -> None:
    source = CanonicalAssetReference(kind="external", uri="https://example.invalid/private.png")

    safe = _safe_asset_references([source])

    assert len(safe) == 1
    assert safe[0].kind == "external"
    assert "uri" not in safe[0].model_dump()
    assert "example.invalid" not in str(safe)


def test_unicode_codepoint_ranges_are_half_open_and_non_empty() -> None:
    text = "题😀答案"
    assert len(text) == 4
    region = _region("/pages/0/blocks/0/content", 2, 4)
    assert text[region["text_start"] : region["text_end"]] == "答案"
    with pytest.raises(ValidationError):
        AnswerReviewDecisionCreate(
            decision="response_present",
            response_regions=[_region("/pages/0/blocks/0/content", 0, 0)],
            reason_codes=["student_work_visible"],
            reviewer_ref="local-reviewer",
        )


def test_synthetic_baseline_is_valid_and_reports_manual_only_metrics() -> None:
    result = evaluate_dataset(_dataset())

    assert result["dataset"]["case_count"] == 13
    manual = result["manual_baseline"]
    assert manual["review_coverage"]["numerator"] == 12
    assert manual["review_coverage"]["denominator"] == 13
    assert manual["uncertain_ratio"]["numerator"] == 3
    assert manual["uncertain_ratio"]["denominator"] == 12
    detector = result["automatic_detector_metrics"]
    assert all(
        detector[name]["status"] == "not_evaluated" and detector[name]["value"] is None
        for name in ("precision", "recall", "false_positive_rate")
    )


def test_detector_metrics_use_separate_predictions_and_hand_computable_counts() -> None:
    dataset = _dataset()
    predictions = {
        case.case_id: "response_present"
        for case in dataset.cases
        if case.decision in {"response_present", "response_absent"}
    }
    absent_case = next(case for case in dataset.cases if case.decision == "response_absent")
    positive_cases = [case for case in dataset.cases if case.decision == "response_present"]
    predictions[absent_case.case_id] = "response_present"
    predictions[positive_cases[0].case_id] = "response_absent"

    result = evaluate_predictions(dataset, predictions)
    assert result["confusion_counts"] == {
        "true_positive": 7,
        "false_positive": 1,
        "false_negative": 1,
        "true_negative": 0,
    }
    assert result["precision"]["value"] == pytest.approx(7 / 8)
    assert result["recall"]["value"] == pytest.approx(7 / 8)
    assert result["false_positive_rate"]["value"] == pytest.approx(1.0)


def test_zero_denominator_metrics_are_not_evaluated() -> None:
    raw = json.loads(DATASET_PATH.read_text())
    raw["cases"] = [case for case in raw["cases"] if case["decision"] in {None, "uncertain"}]
    dataset = AnswerPresenceAnnotationDataset.model_validate(raw)

    result = evaluate_predictions(dataset, {})
    assert result["precision"]["status"] == "not_evaluated"
    assert result["precision"]["denominator"] == 0
    assert result["recall"]["denominator"] == 0
    assert result["false_positive_rate"]["denominator"] == 0


def test_duplicate_case_ids_and_overlapping_regions_are_rejected() -> None:
    raw = json.loads(DATASET_PATH.read_text())
    duplicate_cases = dict(raw)
    duplicate_cases["cases"] = [raw["cases"][0], raw["cases"][0]]
    with pytest.raises(ValidationError, match="case_id"):
        AnswerPresenceAnnotationDataset.model_validate(duplicate_cases)

    overlap = dict(raw["cases"][1])
    overlap["regions"] = [
        {"node_id": "n1", "label": "printed_prompt", "text_start": 0, "text_end": 6},
        {"node_id": "n1", "label": "student_response", "text_start": 5, "text_end": 9},
    ]
    bad_case = dict(raw["cases"][1])
    bad_case.update(overlap)
    bad_dataset = dict(raw)
    bad_dataset["cases"] = [bad_case]
    with pytest.raises(ValidationError, match="overlap"):
        AnswerPresenceAnnotationDataset.model_validate(bad_dataset)


def test_synthetic_source_pointer_must_be_a_real_canonical_path_shape() -> None:
    raw = json.loads(DATASET_PATH.read_text())
    invalid = dict(raw)
    invalid_case = dict(raw["cases"][0])
    invalid_case["source_nodes"] = [
        {**raw["cases"][0]["source_nodes"][0], "content_pointer": "/pages/0/blocks/0/value"}
    ]
    invalid["cases"] = [invalid_case]

    with pytest.raises(ValidationError, match="Canonical node path"):
        AnswerPresenceAnnotationDataset.model_validate(invalid)
