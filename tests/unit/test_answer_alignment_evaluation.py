import importlib.util
import json
import sys
from pathlib import Path

import pytest

_EVALUATOR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_answer_alignment.py"
_EVALUATOR_SPEC = importlib.util.spec_from_file_location(
    "evaluate_answer_alignment", _EVALUATOR_PATH
)
assert _EVALUATOR_SPEC is not None and _EVALUATOR_SPEC.loader is not None
_EVALUATOR = importlib.util.module_from_spec(_EVALUATOR_SPEC)
_EVALUATOR_SPEC.loader.exec_module(_EVALUATOR)
main = _EVALUATOR.main
_answer_interval_counts = _EVALUATOR._answer_interval_counts


def _boundary_case(case_id: str, expected_range: list[int]) -> dict:
    return {
        "case_id": case_id,
        "expected_behavior": "Keep this case's manual answer span separate from other cases.",
        "pages": [[{"source_type": "text", "text": "第1题 ABCDEF"}]],
        "question_numbers": [1],
        "candidate_labels": [
            {
                "question_number": 1,
                "page_index": 0,
                "block_index": 0,
                "marker_start": 0,
                "marker_end": 3,
                "expected_question_number": 1,
            }
        ],
        "confounder_markers": [],
        "answer_labels": [
            {
                "question_number": 1,
                "answer_regions": [
                    {
                        "page_index": 0,
                        "block_index": 0,
                        "start": expected_range[0],
                        "end": expected_range[1],
                    }
                ],
                "expected_status": "aligned",
                "expected_answer_present": True,
                "expected_auto_accept": True,
            }
        ],
        "asset_expectations": [],
        "expected_source_types": [],
    }


def test_offline_boundary_metrics_do_not_mix_same_coordinates_between_cases(
    tmp_path, monkeypatch, capsys
) -> None:
    dataset_path = tmp_path / "same-coordinates.json"
    dataset_path.write_text(
        json.dumps(
            {
                "dataset_name": "case-isolation-regression",
                "annotation_note": "Two synthetic cases with the same source coordinates.",
                "cases": [
                    _boundary_case("case-a", [3, 6]),
                    _boundary_case("case-b", [6, 10]),
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["evaluate_answer_alignment.py", str(dataset_path)])

    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["metric_definition_version"] == "2.0"

    # Each case predicts [3, 10]. Its intersections with the two manual spans
    # are 3 and 4 characters. Across both cases: TP=7, predicted=14, expected=7.
    metrics = report["metrics"]
    assert metrics["answer_boundary_character_precision"]["numerator"] == 7
    assert metrics["answer_boundary_character_precision"]["denominator"] == 14
    assert metrics["answer_boundary_character_precision"]["value"] == 0.5
    assert metrics["answer_boundary_character_recall"]["value"] == 1.0
    assert metrics["answer_boundary_character_iou"]["value"] == 0.5
    assert metrics["answer_boundary_exact_range_precision"]["denominator"] == 2
    assert metrics["answer_boundary_exact_range_recall"]["denominator"] == 2
    assert report["confusion_counts"]["answer_boundary_characters"] == {
        "tp": 7,
        "fp": 7,
        "fn": 0,
    }
    assert report["confusion_counts"]["answer_boundary_exact_ranges"] == {
        "tp": 0,
        "fp": 2,
        "fn": 2,
    }
    assert report["case_failures"] == []


def test_identical_coordinates_in_different_cases_have_no_intersection() -> None:
    expected = {
        ("case-a", 1, 0, 0, 0, 4),
        ("case-b", 1, 0, 0, 4, 8),
    }
    predicted = {
        ("case-a", 1, 0, 0, 4, 8),
        ("case-b", 1, 0, 0, 0, 4),
    }

    assert _answer_interval_counts(expected, predicted) == (0, 8, 8, 16)


def test_overlapping_and_adjacent_ranges_are_unioned_within_one_case() -> None:
    expected = {
        ("case-a", 1, 0, 0, 0, 4),
        ("case-a", 1, 0, 0, 2, 6),
    }
    predicted = {
        ("case-a", 1, 0, 0, 0, 2),
        ("case-a", 1, 0, 0, 2, 6),
    }

    assert _answer_interval_counts(expected, predicted) == (6, 6, 6, 6)


def test_same_coordinates_assigned_to_the_wrong_question_do_not_intersect() -> None:
    expected = {("case-a", 1, 0, 0, 0, 4)}
    predicted = {("case-a", 2, 0, 0, 0, 4)}

    assert _answer_interval_counts(expected, predicted) == (0, 4, 4, 8)


def test_missing_answer_range_reduces_recall_and_iou() -> None:
    expected = {
        ("case-a", 1, 0, 0, 0, 4),
        ("case-a", 1, 0, 0, 8, 12),
    }
    predicted = {("case-a", 1, 0, 0, 0, 4)}

    assert _answer_interval_counts(expected, predicted) == (4, 8, 4, 8)


def test_default_evaluation_reports_prompt_only_and_excludes_empty_asset_ranges(
    monkeypatch, capsys
) -> None:
    dataset_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "answer_alignment" / "annotated.json"
    )
    monkeypatch.setattr(sys, "argv", ["evaluate_answer_alignment.py", str(dataset_path)])

    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    cases = {case["case_id"]: case for case in report["case_results"]}

    prompt_only = cases["printed-question-stem-without-student-response"]
    assert prompt_only["actual_question_statuses"]["1"] == "aligned"
    assert prompt_only["expected_answer_presence"]["1"] is False
    assert prompt_only["answer_presence_assessed"] is False
    assert prompt_only["answer_boundary"]["predicted_range_count"] == 1
    assert report["answer_presence_limitations"][0]["case_id"] == (
        "printed-question-stem-without-student-response"
    )

    table_case = cases["table-and-image-answer"]
    assert table_case["answer_boundary"]["expected_range_count"] == 2
    assert table_case["answer_boundary"]["predicted_range_count"] == 2


def test_evaluator_rejects_duplicate_case_ids(tmp_path, monkeypatch) -> None:
    dataset_path = tmp_path / "duplicate-case-ids.json"
    duplicate_case = _boundary_case("same-case", [3, 6])
    dataset_path.write_text(
        json.dumps(
            {
                "dataset_name": "duplicate-case-ids",
                "annotation_note": "Invalid test fixture.",
                "cases": [duplicate_case, duplicate_case],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["evaluate_answer_alignment.py", str(dataset_path)])

    with pytest.raises(ValueError, match="case_id values must be unique"):
        main()
