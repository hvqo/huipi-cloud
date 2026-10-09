"""Run a small manually annotated synthetic evaluation set for the P2-D1 baseline."""

import json
import re
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from huipi_cloud.modules.answer_alignment.detector import detect_question_candidates
from huipi_cloud.modules.answer_alignment.segmenter import align_canonical_document
from huipi_cloud.modules.assignments.models import Question
from huipi_cloud.modules.canonical_documents.protocol import (
    CanonicalBlock,
    CanonicalContentNode,
    CanonicalDocument,
    CanonicalPage,
)

_POINTER = re.compile(r"^/pages/(\d+)/blocks/(\d+)/content(?:/|$)")


def main() -> int:
    dataset_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "tests/fixtures/answer_alignment/annotated.json"
    )
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    expected_count = predicted_count = correct_candidates = 0
    expected_question_matches = predicted_question_matches = correct_question_matches = 0
    expected_ranges = predicted_ranges = matched_ranges = boundary_union_ranges = 0
    expected_automatically_aligned = automatically_aligned = 0
    review_candidates = total_candidates = 0
    failure_cases: list[dict[str, object]] = []

    for case in dataset["cases"]:
        document, questions, assignment_id = _build_case(case)
        atoms, candidates = detect_question_candidates(document)
        expected = {
            (
                label["question_number"],
                label["page_index"],
                label["block_index"],
                label["marker_start"],
                label["marker_end"],
            )
            for label in case["labels"]
        }
        predicted = {
            (
                candidate.question_number,
                atoms[candidate.atom_index].block.page_index,
                atoms[candidate.atom_index].block.source_block_index,
                candidate.text_start,
                candidate.text_end,
            )
            for candidate in candidates
        }
        expected_count += len(expected)
        predicted_count += len(predicted)
        correct_candidates += len(expected & predicted)
        total_candidates += len(candidates)

        result = align_canonical_document(
            document,
            questions,
            assignment_id=assignment_id,
            canonical_sha256=document.source_sha256,
            alignment_id=uuid5(document.document_id, "offline-evaluation"),
        )
        question_ids = {question.question_number: question.id for question in questions}
        expected_matches = {
            (
                label["question_number"],
                label["page_index"],
                label["block_index"],
                label["marker_start"],
                label["marker_end"],
                str(question_ids[label["question_number"]]),
            )
            for label in case["labels"]
            if label["question_number"] in question_ids
        }
        predicted_matches = set()
        for candidate, evidence in zip(candidates, result.candidates, strict=True):
            question_id = evidence.question_id
            if question_id is None:
                continue
            atom = atoms[candidate.atom_index]
            predicted_matches.add(
                (
                    candidate.question_number,
                    atom.block.page_index,
                    atom.block.source_block_index,
                    candidate.text_start,
                    candidate.text_end,
                    str(question_id),
                )
            )
        expected_question_matches += len(expected_matches)
        predicted_question_matches += len(predicted_matches)
        correct_question_matches += len(expected_matches & predicted_matches)

        answers_by_number = {answer.question_number: answer for answer in result.answers}
        case_expected_ranges = set()
        case_predicted_ranges = set()
        for label in case["labels"]:
            answer = answers_by_number[label["question_number"]]
            case_expected_ranges.update(
                (
                    label["question_number"],
                    region["page_index"],
                    region["block_index"],
                    region["start"],
                    region["end"],
                )
                for region in label["answer_regions"]
            )
        for answer in result.answers:
            for region in answer.source_regions:
                if region.text_start is None or region.text_end is None:
                    continue
                page_index, block_index = _pointer_location(region.content_pointer)
                case_predicted_ranges.add(
                    (
                        answer.question_number,
                        page_index,
                        block_index,
                        region.text_start,
                        region.text_end,
                    )
                )
        expected_ranges += len(case_expected_ranges)
        predicted_ranges += len(case_predicted_ranges)
        matched_ranges += len(case_expected_ranges & case_predicted_ranges)
        boundary_union_ranges += len(case_expected_ranges | case_predicted_ranges)

        expected_numbers = set(case["expected_answered_question_numbers"])
        expected_automatically_aligned += len(expected_numbers)
        automatically_aligned += sum(
            answers_by_number.get(number) is not None
            and answers_by_number[number].matching_status == "aligned"
            for number in expected_numbers
        )
        review_candidates += sum(
            candidate.matching_status == "review_required" for candidate in result.candidates
        )
        if (
            result.status != "complete"
            or any(answer.matching_status != "aligned" for answer in result.answers)
            or result.unassigned_regions
        ):
            reasons = sorted(
                {
                    reason
                    for evidence in result.candidates
                    for reason in evidence.reason_codes
                }
            )
            if any(answer.matching_status == "not_observed" for answer in result.answers):
                reasons.append("no_question_candidate_observed")
            failure_cases.append(
                {"case_id": case["case_id"], "status": result.status, "reasons": reasons}
            )

    candidate_precision = correct_candidates / predicted_count if predicted_count else 0.0
    candidate_recall = correct_candidates / expected_count if expected_count else 0.0
    question_match_precision = (
        correct_question_matches / predicted_question_matches
        if predicted_question_matches
        else 0.0
    )
    question_match_recall = (
        correct_question_matches / expected_question_matches
        if expected_question_matches
        else 0.0
    )
    coverage = (
        automatically_aligned / expected_automatically_aligned
        if expected_automatically_aligned
        else 0.0
    )
    review_rate = review_candidates / total_candidates if total_candidates else 0.0
    boundary_precision = matched_ranges / predicted_ranges if predicted_ranges else 0.0
    boundary_recall = matched_ranges / expected_ranges if expected_ranges else 0.0
    boundary_accuracy = matched_ranges / boundary_union_ranges if boundary_union_ranges else 0.0
    print(
        json.dumps(
            {
                "dataset_name": dataset["dataset_name"],
                "case_count": len(dataset["cases"]),
                "annotation_note": dataset["annotation_note"],
                "candidate_detection_precision": candidate_precision,
                "candidate_detection_recall": candidate_recall,
                "question_match_precision": question_match_precision,
                "question_match_recall": question_match_recall,
                "automatic_alignment_coverage": coverage,
                "review_required_candidate_ratio": review_rate,
                "answer_boundary_exact_precision": boundary_precision,
                "answer_boundary_exact_recall": boundary_recall,
                "answer_boundary_exact_accuracy": boundary_accuracy,
                "counts": {
                    "expected_candidates": expected_count,
                    "predicted_candidates": predicted_count,
                    "correct_candidates": correct_candidates,
                    "expected_question_matches": expected_question_matches,
                    "predicted_question_matches": predicted_question_matches,
                    "correct_question_matches": correct_question_matches,
                    "expected_answer_ranges": expected_ranges,
                    "predicted_answer_ranges": predicted_ranges,
                    "exact_answer_ranges": matched_ranges,
                    "answer_range_union": boundary_union_ranges,
                    "expected_answered_questions": expected_automatically_aligned,
                    "automatically_aligned_questions": automatically_aligned,
                    "review_required_candidates": review_candidates,
                    "all_candidates": total_candidates,
                },
                "failure_cases": failure_cases,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _build_case(case: dict):
    assignment_id = uuid5(NAMESPACE_URL, f"huipi-eval:{case['case_id']}:assignment")
    submission_id = uuid5(NAMESPACE_URL, f"huipi-eval:{case['case_id']}:submission")
    source_artifact_id = uuid5(NAMESPACE_URL, f"huipi-eval:{case['case_id']}:source")
    document_id = uuid5(NAMESPACE_URL, f"huipi-eval:{case['case_id']}:canonical")
    questions = [
        Question(
            id=uuid5(NAMESPACE_URL, f"huipi-eval:{case['case_id']}:question:{number}"),
            assignment_id=assignment_id,
            question_number=number,
            question_type="essay",
            stem=f"评测题 {number}",
        )
        for number in case["question_numbers"]
    ]
    pages = []
    reading_order = 0
    for page_index, specs in enumerate(case["pages"]):
        blocks = []
        for block_index, spec in enumerate(specs):
            source_type = spec["source_type"]
            normalized_type = "layout" if source_type == "paragraph_title" else "text"
            content = CanonicalContentNode(
                normalized_type=normalized_type,
                source_type=source_type,
                content_kind="scalar",
                value=spec["text"],
                source_fields={},
            )
            blocks.append(
                CanonicalBlock(
                    block_id=uuid5(
                        NAMESPACE_URL,
                        f"huipi-eval:{case['case_id']}:block:{page_index}:{block_index}",
                    ),
                    normalized_type=normalized_type,
                    reading_order=reading_order,
                    page_index=page_index,
                    page_number=page_index + 1,
                    source_page_idx=page_index,
                    source_block_index=block_index,
                    source_type=source_type,
                    content=content,
                    asset_refs=[],
                )
            )
            reading_order += 1
        pages.append(
            CanonicalPage(
                page_index=page_index,
                page_number=page_index + 1,
                source_page_idx=page_index,
                blocks=blocks,
                source_fields={},
            )
        )
    document = CanonicalDocument(
        document_id=document_id,
        submission_id=submission_id,
        source_artifact_id=source_artifact_id,
        source_parser="MinerU",
        source_parser_version="4.0.10",
        source_schema_name="docvortex.middle",
        source_schema_version="2.0",
        source_sha256="a" * 64,
        source_middle_json_sha256="b" * 64,
        page_count=len(pages),
        pages=pages,
        assets=[],
        source_metadata={},
    )
    return document, questions, assignment_id


def _pointer_location(pointer: str) -> tuple[int, int]:
    match = _POINTER.match(pointer)
    if match is None:
        raise ValueError("Canonical source pointer has an unexpected shape")
    return int(match.group(1)), int(match.group(2))


if __name__ == "__main__":
    sys.exit(main())
