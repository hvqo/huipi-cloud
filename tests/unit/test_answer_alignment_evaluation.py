import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from huipi_cloud.modules.answer_alignment.detector import detect_question_candidates
from huipi_cloud.modules.canonical_documents.protocol import (
    CanonicalBlock,
    CanonicalContentNode,
    CanonicalDocument,
    CanonicalPage,
)

_EVALUATOR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_answer_alignment.py"
_EVALUATOR_SPEC = importlib.util.spec_from_file_location(
    "evaluate_answer_alignment", _EVALUATOR_PATH
)
assert _EVALUATOR_SPEC is not None and _EVALUATOR_SPEC.loader is not None
_EVALUATOR = importlib.util.module_from_spec(_EVALUATOR_SPEC)
_EVALUATOR_SPEC.loader.exec_module(_EVALUATOR)
main = _EVALUATOR.main
_answer_interval_counts = _EVALUATOR._answer_interval_counts
_merged_range_keys = _EVALUATOR._merged_range_keys


def _nested_text_document() -> CanonicalDocument:
    children = [
        CanonicalContentNode(
            normalized_type="text",
            source_type="text",
            content_kind="scalar",
            value="第1题 AAAA",
            source_fields={},
        ),
        CanonicalContentNode(
            normalized_type="text",
            source_type="text",
            content_kind="scalar",
            value="第1题 BBBB",
            source_fields={},
        ),
    ]
    content = CanonicalContentNode(
        normalized_type="text",
        source_type="text",
        content_kind="children",
        children=children,
        source_fields={},
    )
    block = CanonicalBlock(
        block_id=uuid4(),
        normalized_type="text",
        reading_order=0,
        page_index=0,
        page_number=1,
        source_page_idx=0,
        source_block_index=0,
        source_type="text",
        content=content,
    )
    page = CanonicalPage(
        page_index=0,
        page_number=1,
        source_page_idx=0,
        blocks=[block],
        source_fields={},
    )
    return CanonicalDocument(
        document_id=uuid4(),
        submission_id=uuid4(),
        source_artifact_id=uuid4(),
        source_parser="MinerU",
        source_parser_version="4.0.10",
        source_schema_name="docvortex.middle",
        source_schema_version="2.0",
        source_sha256="a" * 64,
        source_middle_json_sha256="b" * 64,
        page_count=1,
        pages=[page],
        assets=[],
        source_metadata={},
    )


def test_candidate_identity_includes_nested_content_node_path() -> None:
    atoms, candidates = detect_question_candidates(_nested_text_document())
    assert [atom.content_pointer for atom in atoms] == [
        "/pages/0/blocks/0/content/children/0",
        "/pages/0/blocks/0/content/children/1",
    ]
    assert [
        (candidate.question_number, candidate.text_start, candidate.text_end)
        for candidate in candidates
    ] == [(1, 0, 3), (1, 0, 3)]

    candidate_keys = {
        _EVALUATOR._candidate_key(
            {
                "question_number": candidate.question_number,
                "content_pointer": atoms[candidate.atom_index].content_pointer,
                "page_index": atoms[candidate.atom_index].block.page_index,
                "block_index": atoms[candidate.atom_index].block.source_block_index,
                "marker_start": candidate.text_start,
                "marker_end": candidate.text_end,
            }
        )
        for candidate in candidates
    }
    assert len(candidate_keys) == 2
    candidate_link_keys = {
        _EVALUATOR._candidate_question_link_key(
            {
                "question_number": candidate.question_number,
                "content_pointer": atoms[candidate.atom_index].content_pointer,
                "marker_start": candidate.text_start,
                "marker_end": candidate.text_end,
            },
            "question-id",
        )
        for candidate in candidates
    }
    assert len(candidate_link_keys) == 2


def test_nested_answer_ranges_do_not_collapse_same_local_offsets() -> None:
    atoms, _ = detect_question_candidates(_nested_text_document())
    range_keys = {
        (
            "nested-case",
            1,
            atom.content_pointer,
            3,
            7,
        )
        for atom in atoms
    }

    # Each nested node contributes the local half-open range [3, 7).
    assert _answer_interval_counts(range_keys, range_keys) == (8, 8, 8, 8)


def test_overlapping_ranges_merge_only_within_the_same_content_node() -> None:
    pointer0 = "/pages/0/blocks/0/content/children/0"
    pointer1 = "/pages/0/blocks/0/content/children/1"
    expected = {
        ("nested-case", 1, pointer0, 3, 7),
        ("nested-case", 1, pointer0, 5, 9),
        ("nested-case", 1, pointer1, 3, 7),
    }
    predicted = {
        ("nested-case", 1, pointer0, 3, 9),
        ("nested-case", 1, pointer1, 3, 7),
    }

    assert _answer_interval_counts(expected, predicted) == (10, 10, 10, 10)
    assert _merged_range_keys(expected) == _merged_range_keys(predicted) == {
        ("nested-case", 1, pointer0, 3, 9),
        ("nested-case", 1, pointer1, 3, 7),
    }


def test_same_offsets_on_different_content_nodes_do_not_match() -> None:
    expected = {("nested-case", 1, "/pages/0/blocks/0/content/children/0", 3, 7)}
    predicted = {("nested-case", 1, "/pages/0/blocks/0/content/children/1", 3, 7)}

    assert _answer_interval_counts(expected, predicted) == (0, 4, 4, 8)


def test_nested_offsets_count_unicode_code_points_not_utf8_bytes() -> None:
    document = _nested_text_document()
    atoms, _ = detect_question_candidates(document)
    pointer = atoms[0].content_pointer
    text_by_pointer = {atom.content_pointer: atom.text for atom in atoms if atom.text is not None}
    assert len(atoms[0].text) == 8
    assert len(atoms[0].text.encode("utf-8")) > len(atoms[0].text)

    label = {
        "page_index": 0,
        "block_index": 0,
        "content_pointer": pointer,
        "start": 3,
        "end": 8,
    }
    assert _EVALUATOR._label_content_pointer(
        label,
        document,
        text_by_pointer,
        start_key="start",
        end_key="end",
    ) == pointer

    source = {("unicode-case", 1, pointer, 3, 6)}

    assert _answer_interval_counts(source, source) == (3, 3, 3, 3)


def test_separate_nested_fixture_counts_candidates_links_and_ranges(
    monkeypatch, capsys
) -> None:
    dataset_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "answer_alignment"
        / "nested_nodes.json"
    )
    monkeypatch.setattr(sys, "argv", ["evaluate_answer_alignment.py", str(dataset_path)])

    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["case_count"] == 1
    assert report["metric_definition_version"] == "2.1"
    assert report["counts"]["expected_true_candidates"] == 2
    assert report["counts"]["predicted_candidates_including_confounders"] == 2
    assert report["counts"]["correct_candidates"] == 2
    assert report["counts"]["expected_candidate_question_links"] == 2
    assert report["counts"]["predicted_candidate_question_links"] == 2
    assert report["counts"]["correct_candidate_question_links"] == 2
    assert report["counts"]["expected_answer_ranges"] == 2
    assert report["counts"]["predicted_answer_ranges"] == 2
    assert report["counts"]["expected_answer_characters"] == 8
    assert report["counts"]["predicted_answer_characters"] == 8
    assert report["counts"]["overlapping_answer_characters"] == 8
    assert report["metrics"]["answer_boundary_character_iou"]["value"] == 1.0
    assert report["case_failures"] == []


@pytest.mark.parametrize(
    ("pointer", "expected_message"),
    [
        ("/pages/0/blocks/0/content/children/9", "does not identify a text ContentNode"),
        ("/pages/0/blocks/0/content/children/0/extra", "unexpected shape"),
        ("/pages/1/blocks/0/content/children/0", "does not match its page/block location"),
        (None, "nested ContentNode annotations require an explicit content_pointer"),
    ],
)
def test_nested_annotations_reject_unknown_or_implicit_node_paths(
    tmp_path, monkeypatch, pointer, expected_message
) -> None:
    dataset_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "answer_alignment"
        / "nested_nodes.json"
    )
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    marker = dataset["cases"][0]["candidate_labels"][0]
    if pointer is None:
        marker.pop("content_pointer")
    else:
        marker["content_pointer"] = pointer
    invalid_dataset = tmp_path / "invalid-nested-label.json"
    invalid_dataset.write_text(json.dumps(dataset), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["evaluate_answer_alignment.py", str(invalid_dataset)])

    with pytest.raises(ValueError, match=expected_message):
        main()


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
    assert report["metric_definition_version"] == "2.1"

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
    pointer = "/pages/0/blocks/0/content"
    expected = {
        ("case-a", 1, pointer, 0, 4),
        ("case-b", 1, pointer, 4, 8),
    }
    predicted = {
        ("case-a", 1, pointer, 4, 8),
        ("case-b", 1, pointer, 0, 4),
    }

    assert _answer_interval_counts(expected, predicted) == (0, 8, 8, 16)


def test_overlapping_and_adjacent_ranges_are_unioned_within_one_case() -> None:
    pointer = "/pages/0/blocks/0/content"
    expected = {
        ("case-a", 1, pointer, 0, 4),
        ("case-a", 1, pointer, 2, 6),
    }
    predicted = {
        ("case-a", 1, pointer, 0, 2),
        ("case-a", 1, pointer, 2, 6),
    }

    assert _answer_interval_counts(expected, predicted) == (6, 6, 6, 6)


def test_same_coordinates_assigned_to_the_wrong_question_do_not_intersect() -> None:
    pointer = "/pages/0/blocks/0/content"
    expected = {("case-a", 1, pointer, 0, 4)}
    predicted = {("case-a", 2, pointer, 0, 4)}

    assert _answer_interval_counts(expected, predicted) == (0, 4, 4, 8)


def test_missing_answer_range_reduces_recall_and_iou() -> None:
    pointer = "/pages/0/blocks/0/content"
    expected = {
        ("case-a", 1, pointer, 0, 4),
        ("case-a", 1, pointer, 8, 12),
    }
    predicted = {("case-a", 1, pointer, 0, 4)}

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
