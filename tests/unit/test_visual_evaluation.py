from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from huipi_cloud.modules.visual_evaluation.metrics import (
    box_iou,
    evaluate_visual_predictions,
)
from huipi_cloud.modules.visual_evaluation.protocol import (
    VisualEvaluationDataset,
    load_dataset,
)

DATASET_PATH = (
    Path(__file__).resolve().parents[2]
    / "datasets"
    / "visual-evidence"
    / "synthetic-v1"
    / "dataset.json"
)


def _case(
    case_id: str,
    presence: str,
    *,
    attribution_status: str | None = None,
    attribution_question: int | None = None,
    response_question: int | None = None,
    extra_regions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if attribution_status is None:
        attribution_status = {
            "response_present": "target_question",
            "response_absent": "not_applicable",
            "uncertain": "uncertain",
        }[presence]
    regions: list[dict[str, object]] = [
        {
            "region_id": "prompt",
            "page_index": 0,
            "bbox": [0.1, 0.1, 0.5, 0.2],
            "label": "printed_prompt",
            "role": "prompt",
            "question_number": 1,
        }
    ]
    if presence == "response_present":
        regions.append(
            {
                "region_id": "answer",
                "page_index": 0,
                "bbox": [0.1, 0.4, 0.4, 0.6],
                "label": "handwritten_text",
                "role": "response",
                "question_number": response_question or 1,
            }
        )
    if presence == "uncertain":
        regions.append(
            {
                "region_id": "unclear",
                "page_index": 0,
                "bbox": [0.6, 0.4, 0.8, 0.6],
                "label": "unclear",
                "role": "response",
                "question_number": None,
            }
        )
    if extra_regions:
        regions.extend(extra_regions)
    return {
        "case_id": case_id,
        "source_kind": "programmatic_raster",
        "dataset_kind": "synthetic",
        "question_number": 1,
        "question_stem": "Solve the sample question.",
        "pages": [
            {
                "page_index": 0,
                "image_path": "images/sample.png",
                "content_type": "image/png",
                "width": 2,
                "height": 2,
                "sha256": "0" * 64,
            }
        ],
        "manual_answer_presence": presence,
        "question_attribution": {
            "status": attribution_status,
            "question_number": attribution_question,
        },
        "ground_truth_regions": regions,
        "annotation_provenance": "synthetic hand-authored test label",
    }


def _dataset(cases: list[dict[str, object]]) -> VisualEvaluationDataset:
    return VisualEvaluationDataset.model_validate(
        {
            "dataset_id": "unit_test_dataset",
            "dataset_version": "1.0.0",
            "dataset_kind": "synthetic",
            "cases": cases,
        }
    )


def _suggestion(
    case_id: str,
    outcome: str,
    regions: list[dict[str, object]] | None = None,
    *,
    status: str = "ok",
) -> dict[str, object]:
    if regions is None and status == "ok":
        if outcome == "candidate_response_present":
            regions = [
                {
                    "page_index": 0,
                    "visual_bbox": [0.1, 0.4, 0.4, 0.6],
                    "evidence_type": "handwritten_text",
                }
            ]
        elif outcome == "candidate_prompt_only":
            regions = [
                {
                    "page_index": 0,
                    "visual_bbox": [0.1, 0.1, 0.5, 0.2],
                    "evidence_type": "printed_prompt",
                }
            ]
    record: dict[str, object] = {
        "case_id": case_id,
        "status": status,
        "protocol_valid": status == "ok",
        "suggestion": {
            "outcome": outcome,
            "visual_regions": regions or [],
        }
        if status == "ok"
        else None,
        "elapsed_ms": 10,
        "input_digest": "a" * 64 if status == "ok" else None,
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        "rendered_pages": [{"page_index": 0, "width": 2, "height": 2}],
        "source_bytes_total": 12,
    }
    if status != "ok":
        record["error_code"] = "synthetic_failure"
    return record


def _prediction_run(
    dataset: VisualEvaluationDataset,
    records: list[dict[str, object]],
    *,
    dataset_sha256: str = "0" * 64,
) -> dict[str, object]:
    return {
        "schema_name": "huipi.visual.evidence.predictions",
        "schema_version": "1.0",
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": dataset_sha256,
        "model_id": "unit-test-model",
        "cases": records,
    }


def test_committed_synthetic_dataset_has_26_verified_cases() -> None:
    dataset = load_dataset(DATASET_PATH)
    dataset_bytes = DATASET_PATH.read_bytes()

    assert len(dataset.cases) == 26
    assert dataset.dataset_kind == "synthetic"
    assert sum(len(case.pages) for case in dataset.cases) == 27
    assert hashlib.sha256(dataset_bytes).hexdigest() == (
        "7b3ec71e471087e5ae4a039f946caf3bee75f5c6501858982d43687c8b94d026"
    )
    assert {
        presence: sum(case.manual_answer_presence == presence for case in dataset.cases)
        for presence in ("response_present", "response_absent", "uncertain")
    } == {"response_present": 14, "response_absent": 7, "uncertain": 5}
    assert sum(case.manual_answer_presence == "uncertain" for case in dataset.cases) == 5
    assert all(
        "not real student work" in case.annotation_provenance.lower()
        or "programmatically drawn" in case.annotation_provenance.lower()
        for case in dataset.cases
    )


def test_duplicate_case_ids_and_duplicate_regions_are_rejected() -> None:
    one = _case("same", "response_present", attribution_question=1)
    duplicate = _case("same", "response_present", attribution_question=1)
    with pytest.raises(ValidationError, match="case_id values must be unique"):
        _dataset([one, duplicate])

    duplicate_region = {
        "region_id": "answer-copy",
        "page_index": 0,
        "bbox": [0.1, 0.4, 0.4, 0.6],
        "label": "handwritten_text",
        "role": "response",
        "question_number": 1,
    }
    with pytest.raises(ValidationError, match="duplicate ground-truth regions"):
        _dataset(
            [
                _case(
                    "duplicate_region",
                    "response_present",
                    attribution_question=1,
                    extra_regions=[duplicate_region],
                )
            ]
        )


def test_invalid_coordinates_page_and_question_attribution_are_rejected() -> None:
    invalid_box = _case("bad_box", "response_present", attribution_question=1)
    invalid_box["ground_truth_regions"][0]["bbox"] = [0.1, 0.1, 1.2, 0.2]
    with pytest.raises(ValidationError, match="normalized"):
        _dataset([invalid_box])

    missing_page = _case("bad_page", "response_present", attribution_question=1)
    missing_page["ground_truth_regions"][1]["page_index"] = 9
    with pytest.raises(ValidationError, match="missing page"):
        _dataset([missing_page])

    wrong_question = _case(
        "wrong_attribution",
        "response_present",
        attribution_question=2,
    )
    with pytest.raises(ValidationError, match="target-question attribution"):
        _dataset([wrong_question])


def test_dataset_image_sha_and_dimensions_are_verified(tmp_path: Path) -> None:
    root = tmp_path / "dataset"
    image_path = root / "images" / "sample.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), "white").save(image_path)
    raw = _case("file_check", "response_present", attribution_question=1)
    raw["pages"][0]["sha256"] = hashlib.sha256(image_path.read_bytes()).hexdigest()
    raw["pages"][0]["width"] = 2
    raw["pages"][0]["height"] = 2
    dataset_json = root / "dataset.json"
    dataset_json.write_text(
        json.dumps(
            {
                "dataset_id": "test_files",
                "dataset_version": "1.0.0",
                "dataset_kind": "synthetic",
                "cases": [raw],
            }
        ),
        encoding="utf-8",
    )
    assert len(load_dataset(dataset_json).cases) == 1

    raw["pages"][0]["sha256"] = "1" * 64
    dataset_json.write_text(
        json.dumps(
            {
                "dataset_id": "test_files",
                "dataset_version": "1.0.0",
                "dataset_kind": "synthetic",
                "cases": [raw],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="SHA-256"):
        load_dataset(dataset_json)


def test_dataset_rejects_path_traversal_and_changed_dimensions(tmp_path: Path) -> None:
    unsafe_path = _case("unsafe_path", "response_present", attribution_question=1)
    unsafe_path["pages"][0]["image_path"] = "../outside.png"
    with pytest.raises(ValidationError, match="normalized relative"):
        _dataset([unsafe_path])

    root = tmp_path / "dataset"
    image_path = root / "images" / "sample.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), "white").save(image_path)
    changed_dimensions = _case("changed_dimensions", "response_present", attribution_question=1)
    changed_dimensions["pages"][0]["width"] = 3
    changed_dimensions["pages"][0]["sha256"] = hashlib.sha256(image_path.read_bytes()).hexdigest()
    dataset_json = root / "dataset.json"
    dataset_json.write_text(
        json.dumps(
            {
                "dataset_id": "test_files",
                "dataset_version": "1.0.0",
                "dataset_kind": "synthetic",
                "cases": [changed_dimensions],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="dimensions changed"):
        load_dataset(dataset_json)


def test_presence_confusion_matrix_and_abstention_are_hand_calculable() -> None:
    dataset = _dataset(
        [
            _case("positive_hit", "response_present", attribution_question=1),
            _case("positive_candidate_negative", "response_present", attribution_question=1),
            _case("false_positive", "response_absent"),
            _case("negative_abstention", "response_absent"),
            _case("uncertain_truth", "uncertain"),
        ]
    )
    predictions = {
        "cases": [
            _suggestion("positive_hit", "candidate_response_present"),
            _suggestion("positive_candidate_negative", "candidate_prompt_only"),
            _suggestion("false_positive", "candidate_response_present"),
            _suggestion("negative_abstention", "uncertain"),
            _suggestion("uncertain_truth", "candidate_response_present"),
        ]
    }

    metrics = evaluate_visual_predictions(dataset, _prediction_run(dataset, predictions["cases"]))
    presence = metrics["answer_presence"]

    assert presence["confusion_counts"]["tp"] == 1
    assert presence["confusion_counts"]["fp"] == 1
    assert presence["confusion_counts"]["fn"] == 1
    assert presence["confusion_counts"]["tn"] == 0
    assert presence["confusion_counts"]["uncertain_manual_truth_excluded"] == 1
    assert presence["precision"] == {
        "status": "evaluated",
        "value": 0.5,
        "numerator": 1,
        "denominator": 2,
        "reason": None,
    }
    assert presence["recall"]["value"] == pytest.approx(0.5)
    assert presence["false_positive_rate"]["value"] == pytest.approx(1.0)
    assert presence["false_negative_rate"]["value"] == pytest.approx(0.5)
    assert presence["automatic_coverage"]["numerator"] == 3
    assert presence["automatic_coverage"]["denominator"] == 4
    assert presence["automatic_coverage"]["value"] == pytest.approx(0.75)
    assert presence["abstention_rate"]["numerator"] == 1
    assert "not a human absence decision" in presence["negative_class"]


def test_bbox_iou_and_all_region_matching_are_hand_calculable() -> None:
    assert box_iou((0, 0, 0.5, 0.5), (0.25, 0.25, 0.75, 0.75)) == pytest.approx(1 / 7)
    case = _case("two_regions", "response_present", attribution_question=1)
    case["ground_truth_regions"] = [
        {
            "region_id": "target-prompt",
            "page_index": 0,
            "bbox": [0.1, 0.05, 0.5, 0.1],
            "label": "printed_prompt",
            "role": "prompt",
            "question_number": 1,
        },
        {
            "region_id": "first",
            "page_index": 0,
            "bbox": [0.1, 0.1, 0.3, 0.3],
            "label": "handwritten_text",
            "role": "response",
            "question_number": 1,
        },
        {
            "region_id": "second",
            "page_index": 0,
            "bbox": [0.6, 0.6, 0.8, 0.8],
            "label": "handwritten_formula",
            "role": "response",
            "question_number": 1,
        },
    ]
    dataset = _dataset([case])
    predictions = {
        "cases": [
            _suggestion(
                "two_regions",
                "candidate_response_present",
                [
                    {
                        "page_index": 0,
                        "visual_bbox": [0.1, 0.05, 0.5, 0.1],
                        "evidence_type": "printed_prompt",
                    },
                    {
                        "page_index": 0,
                        "visual_bbox": [0.1, 0.1, 0.3, 0.3],
                        "evidence_type": "handwritten_text",
                    },
                    {
                        "page_index": 0,
                        "visual_bbox": [0.12, 0.12, 0.28, 0.28],
                        "evidence_type": "handwritten_text",
                    },
                    {
                        "page_index": 0,
                        "visual_bbox": [0.6, 0.6, 0.8, 0.8],
                        "evidence_type": "geometry_mark",
                    },
                ],
            )
        ]
    }

    metrics = evaluate_visual_predictions(dataset, _prediction_run(dataset, predictions["cases"]))
    regions = metrics["visual_regions"]

    assert regions["counts"]["matched_regions_tp"] == 3
    assert regions["counts"]["unmatched_predictions_fp"] == 1
    assert regions["counts"]["missing_ground_truth_fn"] == 0
    assert regions["localization_precision"]["value"] == pytest.approx(3 / 4)
    assert regions["localization_recall"]["value"] == pytest.approx(1.0)
    assert regions["bbox_iou_mean_on_matches"]["value"] == pytest.approx(1.0)
    assert regions["bbox_iou_mean_on_matches"]["denominator"] == 3
    assert regions["counts"]["region_type_error_count"] == 1
    assert metrics["question_source_attribution"]["status"] == "not_evaluated"
    assert metrics["candidate_question_link"]["status"] == "not_evaluated"
    assert metrics["automatic_accept"]["status"] == "not_evaluated"


def test_response_region_candidate_detection_has_separate_hand_calculable_counts() -> None:
    dataset = _dataset(
        [
            _case("hit", "response_present", attribution_question=1),
            _case("miss", "response_present", attribution_question=1),
            _case("false_alarm", "response_absent"),
            _case("uncertain", "uncertain"),
        ]
    )
    predictions = {
        "cases": [
            _suggestion(
                "hit",
                "candidate_response_present",
                [
                    {
                        "page_index": 0,
                        "visual_bbox": [0.1, 0.4, 0.4, 0.6],
                        "evidence_type": "handwritten_text",
                    }
                ],
            ),
            _suggestion("miss", "candidate_prompt_only"),
            _suggestion(
                "false_alarm",
                "candidate_response_present",
                [
                    {
                        "page_index": 0,
                        "visual_bbox": [0.6, 0.6, 0.8, 0.8],
                        "evidence_type": "geometry_mark",
                    }
                ],
            ),
            _suggestion("uncertain", "uncertain"),
        ]
    }

    detection = evaluate_visual_predictions(
        dataset, _prediction_run(dataset, predictions["cases"])
    )["candidate_detection"]

    assert detection["counts"] == {
        "tp": 1,
        "fp": 1,
        "fn": 1,
        "predicted_candidate_regions": 2,
        "ground_truth_response_regions": 2,
        "eligible_case_count": 3,
        "uncertain_manual_case_count": 1,
    }
    assert detection["precision"]["value"] == pytest.approx(1 / 2)
    assert detection["recall"]["value"] == pytest.approx(1 / 2)
    assert detection["matched_iou_mean"]["value"] == pytest.approx(1.0)


def test_prediction_case_set_must_match_dataset() -> None:
    dataset = _dataset([_case("one", "response_absent")])
    with pytest.raises(ValueError, match="exactly match"):
        evaluate_visual_predictions(dataset, _prediction_run(dataset, []))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_suggestion", "suggestion"),
        ("missing_required_field", "outcome"),
        ("out_of_bounds_bbox", "normalized"),
        ("nonnumeric_bbox", "finite numeric"),
        ("nonfinite_bbox", "finite numeric"),
        ("unknown_page", "not given to the model"),
        ("inconsistent_outcome", "candidate_prompt_only"),
        ("unknown_status", "status"),
        ("nonstring_status", "unknown status"),
    ],
)
def test_stored_success_prediction_must_revalidate_strict_protocol(
    mutation: str,
    message: str,
) -> None:
    dataset = _dataset([_case("one", "response_present", attribution_question=1)])
    record = _suggestion(
        "one",
        "candidate_response_present",
        [
            {
                "page_index": 0,
                "visual_bbox": [0.2, 0.3, 0.4, 0.5],
                "evidence_type": "handwritten_text",
            }
        ],
    )
    suggestion = record["suggestion"]
    assert isinstance(suggestion, dict)
    region = suggestion["visual_regions"][0]
    assert isinstance(region, dict)

    if mutation == "missing_suggestion":
        record.pop("suggestion")
    elif mutation == "missing_required_field":
        suggestion.pop("outcome")
    elif mutation == "out_of_bounds_bbox":
        region["visual_bbox"] = [0.2, 0.3, 1.01, 0.5]
    elif mutation == "nonnumeric_bbox":
        region["visual_bbox"] = [0.2, 0.3, "not-a-number", 0.5]
    elif mutation == "nonfinite_bbox":
        region["visual_bbox"] = [0.2, 0.3, math.nan, 0.5]
    elif mutation == "unknown_page":
        region["page_index"] = 1
    elif mutation == "inconsistent_outcome":
        suggestion["outcome"] = "candidate_prompt_only"
    elif mutation == "unknown_status":
        record["status"] = "legacy_success"
    elif mutation == "nonstring_status":
        record["status"] = []

    run = _prediction_run(dataset, [record])
    with pytest.raises(ValueError, match=message):
        evaluate_visual_predictions(dataset, run, expected_dataset_sha256="0" * 64)


def test_status_ok_without_suggestion_cannot_inflate_protocol_pass_rate() -> None:
    dataset = _dataset([_case("one", "response_absent")])
    record = _suggestion("one", "candidate_prompt_only")
    record.pop("suggestion")

    with pytest.raises(ValueError, match="missing suggestion"):
        evaluate_visual_predictions(dataset, _prediction_run(dataset, [record]))


def test_prediction_run_rejects_duplicate_and_missing_case_records() -> None:
    dataset = _dataset(
        [
            _case("one", "response_absent"),
            _case("two", "response_absent"),
        ]
    )
    duplicate = _suggestion("one", "candidate_prompt_only")
    with pytest.raises(ValueError, match="duplicate case_id"):
        evaluate_visual_predictions(dataset, _prediction_run(dataset, [duplicate, duplicate]))

    with pytest.raises(ValueError, match="exactly match"):
        evaluate_visual_predictions(
            dataset,
            _prediction_run(dataset, [_suggestion("one", "candidate_prompt_only")]),
        )


def test_valid_failure_records_are_not_counted_as_protocol_success() -> None:
    dataset = _dataset(
        [
            _case("protocol", "response_present", attribution_question=1),
            _case("request", "response_absent"),
            _case("input", "uncertain"),
        ]
    )
    records = [
        _suggestion("protocol", "uncertain", status="protocol_error"),
        _suggestion("request", "uncertain", status="request_error"),
        _suggestion("input", "uncertain", status="input_error"),
    ]
    metrics = evaluate_visual_predictions(dataset, _prediction_run(dataset, records))

    assert metrics["run_quality"]["json_protocol_pass_rate"] == {
        "status": "evaluated",
        "value": 0.0,
        "numerator": 0,
        "denominator": 3,
        "reason": None,
    }
    assert metrics["run_quality"]["protocol_failure_count"] == 1
    assert metrics["run_quality"]["request_failure_count"] == 1
    assert metrics["run_quality"]["input_failure_count"] == 1
    assert metrics["answer_presence"]["confusion_counts"]["abstained_positive_truth"] == 1


def test_dataset_version_and_digest_are_checked_before_offline_metrics() -> None:
    dataset = _dataset([_case("one", "response_absent")])
    run = _prediction_run(dataset, [_suggestion("one", "candidate_prompt_only")])
    with pytest.raises(ValueError, match="dataset version"):
        evaluate_visual_predictions(
            dataset,
            {**run, "dataset_version": "0.9.0"},
            expected_dataset_sha256="0" * 64,
        )
    with pytest.raises(ValueError, match="SHA-256"):
        evaluate_visual_predictions(
            dataset,
            run,
            expected_dataset_sha256="1" * 64,
        )


def test_committed_baseline_recalculates_without_mutating_labels_or_predictions(
    tmp_path: Path,
) -> None:
    dataset_path = DATASET_PATH
    prediction_path = (
        dataset_path.parents[1]
        / "runs"
        / "qwen3.5-4b-synthetic-v1.json"
    )
    dataset_bytes = dataset_path.read_bytes()
    prediction_bytes = prediction_path.read_bytes()
    expected_digest = hashlib.sha256(dataset_bytes).hexdigest()
    dataset = load_dataset(dataset_path)
    prediction_run = json.loads(prediction_bytes)

    metrics = evaluate_visual_predictions(
        dataset,
        prediction_run,
        expected_dataset_sha256=expected_digest,
    )

    assert metrics["answer_presence"]["confusion_counts"] == {
        "tp": 8,
        "fp": 0,
        "fn": 6,
        "tn": 7,
        "abstained_positive_truth": 0,
        "abstained_negative_truth": 0,
        "uncertain_manual_truth_excluded": 5,
    }
    assert metrics["candidate_detection"]["counts"]["tp"] == 0
    assert metrics["candidate_detection"]["counts"]["fp"] == 8
    assert metrics["candidate_detection"]["counts"]["fn"] == 14
    assert metrics["visual_regions"]["counts"]["matched_regions_tp"] == 0
    assert metrics["visual_regions"]["counts"]["unmatched_predictions_fp"] == 26
    assert metrics["visual_regions"]["counts"]["missing_ground_truth_fn"] == 54
    assert metrics["visual_regions"]["best_iou_mean_per_ground_truth"] == {
        "status": "evaluated",
        "value": pytest.approx(0.029334777676448164),
        "numerator_sum": pytest.approx(1.5840779945282009),
        "denominator": 54,
        "reason": None,
    }
    assert metrics["run_quality"]["elapsed_ms"]["observed_count"] == 26
    assert metrics["run_quality"]["elapsed_ms"] == {
        "mean": pytest.approx(2927.576923076923),
        "p50": 2661,
        "p95": 3224,
        "observed_count": 26,
    }
    assert metrics["run_quality"]["token_usage"]["observed_count"] == 26
    assert metrics["run_quality"]["token_usage"] == {
        "prompt_tokens_sum": 46153,
        "completion_tokens_sum": 2005,
        "observed_count": 26,
    }

    result_path = tmp_path / "metrics.json"
    cli = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_visual_evidence.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(cli),
            "--evaluate-predictions",
            str(prediction_path),
            "--dataset",
            str(dataset_path),
            "--output",
            str(result_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert (tmp_path / "metrics.metrics.json").is_file()
    regenerated_metrics = json.loads((tmp_path / "metrics.metrics.json").read_text())
    committed_metrics_path = prediction_path.with_suffix(".metrics.json")
    assert regenerated_metrics == json.loads(committed_metrics_path.read_text())

    invalid_run = json.loads(prediction_bytes)
    invalid_record = next(record for record in invalid_run["cases"] if record["status"] == "ok")
    invalid_record.pop("suggestion")
    invalid_path = tmp_path / "invalid-predictions.json"
    invalid_path.write_text(json.dumps(invalid_run), encoding="utf-8")
    invalid_output = tmp_path / "invalid-metrics.json"
    rejected = subprocess.run(
        [
            sys.executable,
            str(cli),
            "--evaluate-predictions",
            str(invalid_path),
            "--dataset",
            str(dataset_path),
            "--output",
            str(invalid_output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 2
    assert "missing suggestion" in rejected.stdout
    assert not (tmp_path / "invalid-metrics.metrics.json").exists()
    assert dataset_path.read_bytes() == dataset_bytes
    assert prediction_path.read_bytes() == prediction_bytes
