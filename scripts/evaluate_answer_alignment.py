"""Evaluate the review-first aligner on a small manually annotated synthetic set."""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from huipi_cloud.modules.answer_alignment.detector import detect_question_candidates
from huipi_cloud.modules.answer_alignment.segmenter import align_canonical_document
from huipi_cloud.modules.assignments.models import Question
from huipi_cloud.modules.canonical_documents.protocol import (
    CanonicalAsset,
    CanonicalAssetReference,
    CanonicalBlock,
    CanonicalContentNode,
    CanonicalDocument,
    CanonicalPage,
)

_POINTER = re.compile(r"^/pages/(\d+)/blocks/(\d+)/content(?:/children/\d+)*$")


def main() -> int:
    dataset_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "tests/fixtures/answer_alignment/annotated.json"
    )
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    case_ids = [case["case_id"] for case in dataset["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("evaluation case_id values must be unique")
    expected_candidates = predicted_candidates = correct_candidates = 0
    expected_question_links = predicted_question_links = correct_question_links = 0
    expected_answer_ranges: set[tuple[str, int, str, int, int]] = set()
    predicted_answer_ranges: set[tuple[str, int, str, int, int]] = set()
    question_status_count = question_status_correct = 0
    candidate_review_count = candidate_total = 0
    expected_assets = preserved_assets = 0
    case_results: list[dict[str, object]] = []
    case_failures: list[str] = []
    boundary_deviation_cases: list[dict[str, object]] = []
    answer_presence_limitations: list[dict[str, object]] = []

    for case in dataset["cases"]:
        case_expected_ranges: set[tuple[str, int, str, int, int]] = set()
        case_predicted_ranges: set[tuple[str, int, str, int, int]] = set()
        document, questions, assignment_id, asset_ids = _build_case(case)
        atoms, candidates = detect_question_candidates(document)
        text_by_pointer = {
            atom.content_pointer: atom.text for atom in atoms if atom.text is not None
        }
        candidate_labels = [
            _label_with_content_pointer(
                label,
                document,
                text_by_pointer,
                start_key="marker_start",
                end_key="marker_end",
            )
            for label in case["candidate_labels"]
        ]
        confounder_labels = [
            _label_with_content_pointer(
                label,
                document,
                text_by_pointer,
                start_key="marker_start",
                end_key="marker_end",
            )
            for label in case["confounder_markers"]
        ]
        expected_marker_locations = {
            _candidate_key(label) for label in candidate_labels
        }
        predicted_marker_locations = {
            _candidate_key(
                {
                    "question_number": candidate.question_number,
                    "content_pointer": atoms[candidate.atom_index].content_pointer,
                    "marker_start": candidate.text_start,
                    "marker_end": candidate.text_end,
                }
            )
            for candidate in candidates
        }
        expected_confounder_locations = {
            _candidate_key(marker) for marker in confounder_labels
        }
        expected_candidates += len(expected_marker_locations)
        predicted_candidates += len(predicted_marker_locations)
        correct_candidates += len(expected_marker_locations & predicted_marker_locations)

        result = align_canonical_document(
            document,
            questions,
            assignment_id=assignment_id,
            canonical_sha256=document.source_sha256,
            alignment_id=uuid5(document.document_id, "offline-evaluation"),
        )
        candidate_review_count += sum(
            candidate.matching_status == "review_required" for candidate in result.candidates
        )
        candidate_total += len(result.candidates)
        question_ids = {question.question_number: question.id for question in questions}
        expected_links = set()
        for label in candidate_labels:
            expected_question_number = label.get("expected_question_number")
            if expected_question_number in question_ids:
                expected_links.add(
                    _candidate_question_link_key(
                        label,
                        str(question_ids[expected_question_number]),
                    )
                )
        predicted_links = set()
        for candidate, evidence in zip(candidates, result.candidates, strict=True):
            if evidence.question_id is None:
                continue
            atom = atoms[candidate.atom_index]
            predicted_links.add(
                _candidate_question_link_key(
                    {
                        "question_number": candidate.question_number,
                        "content_pointer": atom.content_pointer,
                        "marker_start": candidate.text_start,
                        "marker_end": candidate.text_end,
                    },
                    str(evidence.question_id),
                )
            )
        expected_question_links += len(expected_links)
        predicted_question_links += len(predicted_links)
        correct_question_links += len(expected_links & predicted_links)

        answer_labels = {label["question_number"]: label for label in case["answer_labels"]}
        answers_by_number = {answer.question_number: answer for answer in result.answers}
        status_matches: dict[str, bool] = {}
        for number, label in answer_labels.items():
            actual = answers_by_number[number].matching_status
            matched = actual == label["expected_status"]
            status_matches[str(number)] = matched
            question_status_count += 1
            question_status_correct += int(matched)
            if not matched:
                case_failures.append(
                    f"{case['case_id']}: Question {number} expected "
                    f"{label['expected_status']}, received {actual}"
                )
            if not label["expected_answer_present"] and actual == "aligned":
                answer_presence_limitations.append(
                    {
                        "case_id": case["case_id"],
                        "question_number": number,
                        "expected_answer_present": False,
                        "observed_alignment_status": actual,
                        "mapped_source_text": answers_by_number[number].text_projection,
                        "reason": (
                            "当前协议只表示来源区域与 Question 的关联；"
                            "不检测学生是否作答。"
                        ),
                    }
                )
            for region in label["answer_regions"]:
                pointer = _label_content_pointer(
                    region,
                    document,
                    text_by_pointer,
                    start_key="start",
                    end_key="end",
                )
                answer_range = (
                    case["case_id"],
                    number,
                    pointer,
                    region["start"],
                    region["end"],
                )
                expected_answer_ranges.add(answer_range)
                case_expected_ranges.add(answer_range)
        for answer in result.answers:
            for region in answer.source_regions:
                if (
                    region.text_start is None
                    or region.text_end is None
                    or region.text_end <= region.text_start
                ):
                    continue
                predicted_answer_ranges.add(
                    (
                        case["case_id"],
                        answer.question_number,
                        region.content_pointer,
                        region.text_start,
                        region.text_end,
                    )
                )
                case_predicted_ranges.add(
                    (
                        case["case_id"],
                        answer.question_number,
                        region.content_pointer,
                        region.text_start,
                        region.text_end,
                    )
                )

        seen_asset_ids = {
            reference.asset_id
            for answer in result.answers
            for reference in answer.asset_refs
            if reference.asset_id is not None
        }
        seen_asset_ids.update(
            reference.asset_id
            for region in result.unassigned_regions
            for source_region in region.source_regions
            for reference in source_region.asset_refs
            if reference.asset_id is not None
        )
        case_expected_asset_ids = set(asset_ids.values())
        expected_assets += len(case_expected_asset_ids)
        preserved_assets += len(case_expected_asset_ids & seen_asset_ids)
        asset_results: list[dict[str, object]] = []
        for expectation in case["asset_expectations"]:
            asset_id = asset_ids[expectation["asset_name"]]
            actual_answer_questions = sorted(
                answer.question_number
                for answer in result.answers
                if any(reference.asset_id == asset_id for reference in answer.asset_refs)
            )
            actual_review_questions = sorted(
                answer.question_number
                for answer in result.answers
                if answer.matching_status == "review_required"
                and answer.question_number in actual_answer_questions
            )
            expected_answer_questions = sorted(expectation["expected_answer_questions"])
            asset_ok = (
                asset_id in seen_asset_ids
                and actual_answer_questions == expected_answer_questions
                and actual_review_questions == sorted(expectation["review_required_questions"])
            )
            asset_results.append(
                {
                    "asset_name": expectation["asset_name"],
                    "preserved": asset_id in seen_asset_ids,
                    "expected_answer_questions": expected_answer_questions,
                    "actual_answer_questions": actual_answer_questions,
                    "review_required_questions": actual_review_questions,
                    "expectation_met": asset_ok,
                }
            )
            if not asset_ok:
                case_failures.append(
                    f"{case['case_id']}: asset attribution expectation failed for "
                    f"{expectation['asset_name']}"
                )

        actual_source_types = {
            region.normalized_type
            for answer in result.answers
            for region in answer.source_regions
        }
        actual_source_types.update(
            source_region.normalized_type
            for region in result.unassigned_regions
            for source_region in region.source_regions
        )
        missing_source_types = sorted(set(case["expected_source_types"]) - actual_source_types)
        if missing_source_types:
            case_failures.append(
                f"{case['case_id']}: missing source types {', '.join(missing_source_types)}"
            )

        (
            case_intersection,
            case_expected_characters,
            case_predicted_characters,
            case_union_characters,
        ) = _answer_interval_counts(case_expected_ranges, case_predicted_ranges)
        merged_case_expected_ranges = _merged_range_keys(case_expected_ranges)
        merged_case_predicted_ranges = _merged_range_keys(case_predicted_ranges)
        case_exact_ranges = len(merged_case_expected_ranges & merged_case_predicted_ranges)
        case_boundary_matches = merged_case_expected_ranges == merged_case_predicted_ranges
        if not case_boundary_matches:
            if not case_expected_ranges and case_predicted_ranges:
                boundary_reason = (
                    "人工标签未确认学生作答，但系统仍输出映射来源片段；"
                    "当前算法不判断答案存在性。"
                )
            elif case_expected_ranges and not case_predicted_ranges:
                boundary_reason = "人工标签有答案区间，但系统没有输出已关联到 Question 的文本区间。"
            else:
                boundary_reason = "预测答案范围与人工标注范围存在差异；检查截断、漏分或多分配。"
            boundary_deviation_cases.append(
                {
                    "case_id": case["case_id"],
                    "reason": boundary_reason,
                    "expected_ranges": [
                        list(answer_range[1:])
                        for answer_range in sorted(merged_case_expected_ranges)
                    ],
                    "predicted_ranges": [
                        list(answer_range[1:])
                        for answer_range in sorted(merged_case_predicted_ranges)
                    ],
                    "overlapping_characters": case_intersection,
                    "expected_characters": case_expected_characters,
                    "predicted_characters": case_predicted_characters,
                }
            )

        case_results.append(
            {
                "case_id": case["case_id"],
                "expected_behavior": case["expected_behavior"],
                "document_status": result.status,
                "expected_question_statuses": {
                    str(number): label["expected_status"]
                    for number, label in answer_labels.items()
                },
                "actual_question_statuses": {
                    str(number): answer.matching_status
                    for number, answer in answers_by_number.items()
                },
                "question_status_expectations_met": status_matches,
                "expected_answer_presence": {
                    str(number): label["expected_answer_present"]
                    for number, label in answer_labels.items()
                },
                "answer_presence_assessed": False,
                "answer_boundary": {
                    "exact_ranges_match": case_boundary_matches,
                    "expected_range_count": len(merged_case_expected_ranges),
                    "predicted_range_count": len(merged_case_predicted_ranges),
                    "character_precision": _metric(
                        case_intersection,
                        case_predicted_characters,
                        "本案例按 Question 和完整 content_pointer 计算的重叠字符 / 预测字符。",
                    ),
                    "character_recall": _metric(
                        case_intersection,
                        case_expected_characters,
                        "本案例按 Question 和完整 content_pointer 计算的重叠字符 / 人工标注字符。",
                    ),
                    "character_iou": _metric(
                        case_intersection,
                        case_union_characters,
                        "本案例按 Question 和完整 content_pointer 计算的重叠字符 / 并集字符。",
                    ),
                    "exact_range_precision": _metric(
                        case_exact_ranges,
                        len(merged_case_predicted_ranges),
                        "本案例完全一致的节点级归并区间数 / 预测区间数。",
                    ),
                    "exact_range_recall": _metric(
                        case_exact_ranges,
                        len(merged_case_expected_ranges),
                        "本案例完全一致的节点级归并区间数 / 人工标注区间数。",
                    ),
                },
                "candidate_count": {
                    "expected_true_markers": len(expected_marker_locations),
                    "detected_markers": len(predicted_marker_locations),
                    "annotated_confounders_detected": len(
                        expected_confounder_locations & predicted_marker_locations
                    ),
                    "annotated_confounders": len(expected_confounder_locations),
                },
                "unassigned_regions": [
                    {
                        "reason_code": region.reason_code,
                        "text_projection": region.text_projection,
                        "source_region_count": len(region.source_regions),
                    }
                    for region in result.unassigned_regions
                ],
                "asset_expectations": asset_results,
                "expected_source_types": case["expected_source_types"],
                "actual_preserved_source_types": sorted(actual_source_types),
            }
        )

    # Candidate and question-number labels repeat between cases. Keep counts
    # case-scoped when evaluating automatic acceptance.
    auto_tp = auto_predicted = auto_expected = 0
    answered_auto_tp = answered_total = 0
    for case_result, case in zip(case_results, dataset["cases"], strict=True):
        expected = {
            number
            for number, label in {
                item["question_number"]: item for item in case["answer_labels"]
            }.items()
            if label["expected_auto_accept"]
        }
        actual = {
            int(number)
            for number, status in case_result["actual_question_statuses"].items()
            if status == "aligned"
        }
        auto_tp += len(expected & actual)
        auto_predicted += len(actual)
        auto_expected += len(expected)
        answered = {
            item["question_number"]
            for item in case["answer_labels"]
            if item["expected_answer_present"]
        }
        answered_auto_tp += len(answered & expected & actual)
        answered_total += len(answered)

    merged_expected_answer_ranges = _merged_range_keys(expected_answer_ranges)
    merged_predicted_answer_ranges = _merged_range_keys(predicted_answer_ranges)
    exact_ranges = len(merged_expected_answer_ranges & merged_predicted_answer_ranges)
    interval_intersection, expected_characters, predicted_characters, union_characters = (
        _answer_interval_counts(expected_answer_ranges, predicted_answer_ranges)
    )
    report = {
        "dataset_name": dataset["dataset_name"],
        "metric_definition_version": "2.1",
        "metric_definition_note": (
            "来源身份按 case_id、Question 和完整 Canonical ContentNode content_pointer 隔离；"
            "字符偏移是相对该节点文本的 Python Unicode 码点半开区间 [start, end)。"
            "只合并同一节点内重叠或相邻的区间，不同节点的相同偏移不匹配；"
            "其他既有报告字段保持兼容。`aligned` 只表示来源到 Question 的映射被接受，"
            "不表示学生作答存在或结果可以进入批改。"
        ),
        "case_count": len(dataset["cases"]),
        "annotation_note": dataset["annotation_note"],
        "metrics": {
            "candidate_detection_precision": _metric(
                correct_candidates,
                predicted_candidates,
                "精确匹配案例内题号、完整 content_pointer 和节点本地字符范围的候选 / "
                "系统输出的全部候选；"
                "误识别列表项和未知伪题号计为误报。",
            ),
            "candidate_detection_recall": _metric(
                correct_candidates,
                expected_candidates,
                "精确匹配案例内题号、完整 content_pointer 和节点本地字符范围的候选 / "
                "人工标注候选总数。",
            ),
            "candidate_question_link_precision": _metric(
                correct_question_links,
                predicted_question_links,
                "案例内候选来源节点与目标 Question 都正确的关联 / 系统产生的全部 Question 关联；"
                "复核候选仍在此候选级指标内。",
            ),
            "candidate_question_link_recall": _metric(
                correct_question_links,
                expected_question_links,
                "案例内候选来源节点与目标 Question 都正确的关联 / 人工标注应关联候选数。",
            ),
            "automatic_accept_precision": _metric(
                auto_tp,
                auto_predicted,
                "人工确认可自动接受来源到 Question 映射且系统标记 aligned 的题目数 / "
                "系统标记 aligned 的题目数；不评价学生是否作答或是否可批改；没有预测时为 null。",
            ),
            "automatic_accept_recall": _metric(
                auto_tp,
                auto_expected,
                "人工确认可自动接受来源到 Question 映射且系统标记 aligned 的题目数 / "
                "人工确认可自动接受的映射数；不评价学生是否作答或是否可批改。",
            ),
            "automatic_alignment_coverage_of_answered_questions": _metric(
                answered_auto_tp,
                answered_total,
                "有人工标注答案且被系统正确标为 aligned 的题数 / 人工标注存在答案的题数；"
                "不代表识别准确率。",
            ),
            "answer_boundary_character_precision": _metric(
                interval_intersection,
                predicted_characters,
                "逐案例按 Question 和完整 content_pointer 计算的重叠字符数 / 系统归属字符数；"
                "错归题目、跨案例或跨节点坐标相同都不构成交集。",
            ),
            "answer_boundary_character_recall": _metric(
                interval_intersection,
                expected_characters,
                "逐案例按 Question 和完整 content_pointer 计算的重叠字符数 / "
                "人工标注字符数；遗漏内容降低召回。",
            ),
            "answer_boundary_character_iou": _metric(
                interval_intersection,
                union_characters,
                "逐案例按 Question 和完整 content_pointer 计算的重叠字符数 / "
                "预测与人工范围并集字符数；"
                "同时惩罚遗漏和多分配内容。",
            ),
            "answer_boundary_exact_range_precision": _metric(
                exact_ranges,
                len(merged_predicted_answer_ranges),
                "完全相同的案例/题号/ContentNode content_pointer/归并半开区间数 / "
                "系统输出的归并文本区间数。",
            ),
            "answer_boundary_exact_range_recall": _metric(
                exact_ranges,
                len(merged_expected_answer_ranges),
                "完全相同的案例/题号/ContentNode content_pointer/归并半开区间数 / "
                "人工标注的归并文本区间数。",
            ),
            "question_status_exact_accuracy": _metric(
                question_status_correct,
                question_status_count,
                "匹配人工标注状态的题目数 / 有人工状态标签的题目数。",
            ),
            "canonical_asset_reference_preservation": _metric(
                preserved_assets,
                expected_assets,
                "在答案区域或未分配区域中仍可见的逻辑素材 ID 数 / 测试集 Canonical 素材 ID 数。",
            ),
            "review_required_candidate_ratio": _metric(
                candidate_review_count,
                candidate_total,
                "状态为 review_required 的检测候选数 / 检测候选总数。",
            ),
        },
        "counts": {
            "expected_true_candidates": expected_candidates,
            "predicted_candidates_including_confounders": predicted_candidates,
            "correct_candidates": correct_candidates,
            "expected_candidate_question_links": expected_question_links,
            "predicted_candidate_question_links": predicted_question_links,
            "correct_candidate_question_links": correct_question_links,
            "expected_auto_accept_questions": auto_expected,
            "predicted_aligned_questions": auto_predicted,
            "correct_auto_accept_questions": auto_tp,
                "expected_answer_ranges": len(merged_expected_answer_ranges),
                "predicted_answer_ranges": len(merged_predicted_answer_ranges),
            "exact_answer_ranges": exact_ranges,
            "expected_answer_characters": expected_characters,
            "predicted_answer_characters": predicted_characters,
            "overlapping_answer_characters": interval_intersection,
            "answer_boundary_union_characters": union_characters,
            "answer_states_compared": question_status_count,
            "answer_states_matching": question_status_correct,
            "expected_asset_references": expected_assets,
            "preserved_asset_references": preserved_assets,
        },
        "confusion_counts": {
            "candidate_detection": {
                "tp": correct_candidates,
                "fp": predicted_candidates - correct_candidates,
                "fn": expected_candidates - correct_candidates,
            },
            "candidate_question_link": {
                "tp": correct_question_links,
                "fp": predicted_question_links - correct_question_links,
                "fn": expected_question_links - correct_question_links,
            },
            "automatic_accept": {
                "tp": auto_tp,
                "fp": auto_predicted - auto_tp,
                "fn": auto_expected - auto_tp,
            },
            "answer_boundary_characters": {
                "tp": interval_intersection,
                "fp": predicted_characters - interval_intersection,
                "fn": expected_characters - interval_intersection,
            },
            "answer_boundary_exact_ranges": {
                "tp": exact_ranges,
                "fp": len(merged_predicted_answer_ranges) - exact_ranges,
                "fn": len(merged_expected_answer_ranges) - exact_ranges,
            },
        },
        "case_results": case_results,
        "boundary_deviation_cases": boundary_deviation_cases,
        "answer_presence_limitations": answer_presence_limitations,
        "case_failures": case_failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if case_failures else 0


def _candidate_key(label: dict) -> tuple[int, str, int, int]:
    content_pointer = label["content_pointer"]
    _pointer_location(content_pointer)
    return (
        label["question_number"],
        content_pointer,
        label["marker_start"],
        label["marker_end"],
    )


def _candidate_question_link_key(label: dict, question_id: str) -> tuple[int, str, int, int, str]:
    return (*_candidate_key(label), question_id)


def _label_with_content_pointer(
    label: dict,
    document: CanonicalDocument,
    text_by_pointer: dict[str, str],
    *,
    start_key: str,
    end_key: str,
) -> dict:
    normalized = dict(label)
    normalized["content_pointer"] = _label_content_pointer(
        label,
        document,
        text_by_pointer,
        start_key=start_key,
        end_key=end_key,
    )
    return normalized


def _label_content_pointer(
    label: dict,
    document: CanonicalDocument,
    text_by_pointer: dict[str, str],
    *,
    start_key: str,
    end_key: str,
) -> str:
    page_index = label.get("page_index")
    block_index = label.get("block_index")
    if (
        isinstance(page_index, bool)
        or not isinstance(page_index, int)
        or isinstance(block_index, bool)
        or not isinstance(block_index, int)
        or page_index < 0
        or page_index >= len(document.pages)
        or block_index < 0
        or block_index >= len(document.pages[page_index].blocks)
    ):
        raise ValueError("annotation page_index/block_index must resolve to a Canonical block")

    block = document.pages[page_index].blocks[block_index]
    if "content_pointer" in label:
        pointer = label["content_pointer"]
        if not isinstance(pointer, str):
            raise ValueError("annotation content_pointer must be a string")
    else:
        if block.content.content_kind != "scalar" or block.content.children:
            raise ValueError("nested ContentNode annotations require an explicit content_pointer")
        pointer = f"/pages/{page_index}/blocks/{block_index}/content"

    if _pointer_location(pointer) != (page_index, block_index):
        raise ValueError("annotation content_pointer does not match its page/block location")
    text = text_by_pointer.get(pointer)
    if text is None:
        raise ValueError("annotation content_pointer does not identify a text ContentNode")

    start = label.get(start_key)
    end = label.get(end_key)
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or end < start
        or end > len(text)
    ):
        raise ValueError("annotation offsets must be valid Unicode code-point half-open ranges")
    return pointer


def _metric(numerator: int, denominator: int, definition: str) -> dict[str, object]:
    return {
        "value": numerator / denominator if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "definition": definition,
    }


def _answer_interval_counts(expected_ranges, predicted_ranges) -> tuple[int, int, int, int]:
    expected = _merge_by_source(expected_ranges)
    predicted = _merge_by_source(predicted_ranges)
    intersection = 0
    for key in expected.keys() & predicted.keys():
        for expected_start, expected_end in expected[key]:
            for predicted_start, predicted_end in predicted[key]:
                intersection += max(
                    0,
                    min(expected_end, predicted_end) - max(expected_start, predicted_start),
                )
    expected_length = sum(
        end - start for intervals in expected.values() for start, end in intervals
    )
    predicted_length = sum(
        end - start for intervals in predicted.values() for start, end in intervals
    )
    union_length = expected_length + predicted_length - intersection
    return intersection, expected_length, predicted_length, union_length


def _merge_by_source(answer_ranges):
    grouped: dict[tuple[str, int, str], list[tuple[int, int]]] = defaultdict(list)
    for case_id, question_number, content_pointer, start, end in answer_ranges:
        _pointer_location(content_pointer)
        if end < start:
            raise ValueError("answer range end must not precede start")
        if end > start:
            grouped[(case_id, question_number, content_pointer)].append((start, end))
    merged = {}
    for key, intervals in grouped.items():
        intervals.sort()
        result: list[list[int]] = []
        for start, end in intervals:
            if result and start <= result[-1][1]:
                result[-1][1] = max(result[-1][1], end)
            else:
                result.append([start, end])
        merged[key] = [(start, end) for start, end in result]
    return merged


def _merged_range_keys(answer_ranges) -> set[tuple[str, int, str, int, int]]:
    return {
        (case_id, question_number, content_pointer, start, end)
        for (case_id, question_number, content_pointer), intervals in _merge_by_source(
            answer_ranges
        ).items()
        for start, end in intervals
    }


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
    assets: dict[str, CanonicalAsset] = {}
    asset_ids: dict[str, UUID] = {}
    reading_order = 0
    for page_index, specs in enumerate(case["pages"]):
        blocks = []
        for block_index, spec in enumerate(specs):
            source_type = spec["source_type"]
            normalized_type = spec.get("normalized_type", _normalized_for_source(source_type))
            references = []
            for asset_name in spec.get("asset_names", []):
                asset_id = asset_ids.setdefault(
                    asset_name,
                    uuid5(NAMESPACE_URL, f"huipi-eval:{case['case_id']}:asset:{asset_name}"),
                )
                references.append(CanonicalAssetReference(kind="stored", asset_id=asset_id))
                assets.setdefault(
                    asset_name,
                    CanonicalAsset(
                        asset_id=asset_id,
                        source_path=f"images/{asset_name}.png",
                        sha256="c" * 64,
                        size_bytes=1,
                        content_type="image/png",
                    ),
                )
            content = _build_content_node(spec, asset_refs=references)
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
                    asset_refs=references,
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
        assets=list(assets.values()),
        source_metadata={},
    )
    return document, questions, assignment_id, asset_ids


def _build_content_node(
    spec: dict,
    *,
    asset_refs: list[CanonicalAssetReference] | None = None,
) -> CanonicalContentNode:
    source_type = spec.get("source_type", "text")
    normalized_type = spec.get("normalized_type", _normalized_for_source(source_type))
    references = asset_refs or []
    if "children" in spec:
        if "text" in spec:
            raise ValueError("a children ContentNode cannot also define text")
        return CanonicalContentNode(
            normalized_type=normalized_type,
            source_type=source_type,
            content_kind="children",
            children=[_build_content_node(child) for child in spec["children"]],
            asset_refs=references,
            source_fields={},
        )
    if "text" not in spec or not isinstance(spec["text"], str):
        raise ValueError("each scalar ContentNode annotation requires string text")
    return CanonicalContentNode(
        normalized_type=normalized_type,
        source_type=source_type,
        content_kind="scalar",
        value=spec["text"],
        asset_refs=references,
        source_fields={},
    )


def _normalized_for_source(source_type: str) -> str:
    return {
        "equation": "formula",
        "equation_inline": "formula",
        "image": "image",
        "table": "table",
        "table_body": "table",
        "chart": "chart",
        "list": "list",
        "page_number": "layout",
        "paragraph_title": "layout",
    }.get(source_type, "text")


def _pointer_location(pointer: str) -> tuple[int, int]:
    match = _POINTER.fullmatch(pointer)
    if match is None:
        raise ValueError("Canonical source pointer has an unexpected shape")
    return int(match.group(1)), int(match.group(2))


if __name__ == "__main__":
    sys.exit(main())
