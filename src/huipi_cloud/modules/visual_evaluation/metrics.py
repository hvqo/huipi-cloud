"""Transparent metrics for model candidates, boxes, and source-attribution evidence."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from pydantic import ValidationError

from huipi_cloud.modules.visual_evaluation.protocol import (
    VisualEvaluationCase,
    VisualEvaluationDataset,
)
from huipi_cloud.modules.visual_evidence.protocol import VisualEvidenceModelReply

PREDICTION_SCHEMA_NAME = "huipi.visual.evidence.predictions"
PREDICTION_SCHEMA_VERSION = "1.0"
_PREDICTION_STATUSES = {"ok", "protocol_error", "request_error", "input_error"}
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _ratio(numerator: int, denominator: int, reason: str) -> dict[str, Any]:
    return {
        "status": "evaluated" if denominator else "not_evaluated",
        "value": numerator / denominator if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "reason": None if denominator else reason,
    }


def box_iou(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def evaluate_visual_predictions(
    dataset: VisualEvaluationDataset,
    prediction_run: dict[str, Any],
    *,
    iou_threshold: float = 0.5,
    expected_dataset_sha256: str | None = None,
) -> dict[str, Any]:
    """Compare stored model outputs to labels, without mixing the two data sources."""

    if not math.isfinite(iou_threshold) or not 0 < iou_threshold <= 1:
        raise ValueError("IoU threshold must be finite and in (0, 1]")
    by_id = _validate_prediction_run(
        dataset,
        prediction_run,
        expected_dataset_sha256=expected_dataset_sha256,
    )

    presence = _presence_metrics(dataset, by_id)
    regions = _region_metrics(dataset, by_id, iou_threshold)
    candidate_detection = _candidate_detection_metrics(dataset, by_id, iou_threshold)
    quality = _quality_metrics(dataset, by_id)
    failures = _case_failures(dataset, by_id, regions["case_details"])
    return {
        "schema_name": "huipi.visual.evidence.evaluation",
        "schema_version": "1.0",
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.dataset_version,
            "dataset_kind": dataset.dataset_kind,
            "case_count": len(dataset.cases),
            "source_image_count": sum(len(case.pages) for case in dataset.cases),
            "manual_label_counts": dict(
                sorted(Counter(case.manual_answer_presence for case in dataset.cases).items())
            ),
            "region_label_counts": dict(
                sorted(
                    Counter(
                        region.label
                        for case in dataset.cases
                        for region in case.ground_truth_regions
                    ).items()
                )
            ),
        },
        "answer_presence": presence,
        "candidate_detection": candidate_detection,
        "visual_regions": regions,
        "candidate_question_link": {
            "status": "not_evaluated",
            "precision": None,
            "recall": None,
            "counts": None,
            "reason": (
                "visual-only dataset has no Canonical/AnswerAlignment Question-link output "
                "or trusted source-pointer labels"
            ),
        },
        "automatic_accept": {
            "status": "not_evaluated",
            "precision": None,
            "recall": None,
            "counts": None,
            "reason": (
                "the provider only returns candidates; the dataset has no verified "
                "Canonical/AnswerAlignment source mapping labels"
            ),
        },
        "question_source_attribution": {
            "status": "not_evaluated",
            "successful_candidate_mappings": None,
            "correct_mappings": None,
            "wrong_question_attributions": None,
            "unmapped_regions": None,
            "shared_figure_abstentions": None,
            "coordinate_transform_refusals": None,
            "reason": "visual-only dataset has no Canonical/AnswerAlignment runtime context",
        },
        "run_quality": quality,
        "failures": failures,
        "metric_definitions": {
            "manual_answer_presence": (
                "是否存在属于该案例目标 Question 的学生作答证据；"
                "response_absent 不表示整页无印刷内容。"
            ),
            "candidate_response_present": "映射为模型候选正类。",
            "candidate_prompt_only": (
                "映射为模型候选负类，仅用于该分类评测；不等于人工确认 response_absent。"
            ),
            "uncertain_or_request_failure": "弃权；不计为正确负类。",
            "accepted_subset": "仅用确定输出与确定人工标签计算；同时报告覆盖率和弃权率。",
            "bbox_coordinates": "页面宽高归一化的半开矩形坐标 [x0,y0,x1,y1]。",
            "region_matching": (
                "每个案例内只比较目标 Question 的人工区域与全部模型区域；按 IoU 从高到低排序，"
                "再按预测下标、标签下标打破平局，一对一贪心匹配。"
            ),
            "source_attribution": (
                "必须有运行时 Canonical/AnswerAlignment 上下文；本视觉样本集没有该上下文，"
                "所以来源归属指标为 not_evaluated。"
            ),
            "candidate_detection": (
                "目标 Question 的人工 response 区域与非 printed_prompt 模型区域按页面和 IoU "
                "阈值做确定性一对一匹配；uncertain 人工案例不计入分母。"
            ),
            "candidate_question_link_and_automatic_accept": (
                "当前视觉数据集没有 Canonical/AnswerAlignment 来源指针真值；"
                "即使视觉区域与目标题一致，"
                "也不据此推断来源链接正确，故两项为 not_evaluated。"
            ),
            "labels_are_predictions": False,
        },
        "limitations": [
            "程序绘制的蓝色笔画不是人类真实笔迹。",
            "合成样本基线不代表真实教学作业准确率。",
            "模型候选和人工复核决定是不同数据；评测不写入 AnswerReviewDecision。",
        ],
    }


def _predicted_presence(record: dict[str, Any]) -> str | None:
    if record.get("status") != "ok":
        return None
    suggestion = record.get("suggestion")
    if not isinstance(suggestion, dict):
        return None
    outcome = suggestion.get("outcome")
    if outcome == "candidate_response_present":
        return "response_present"
    if outcome == "candidate_prompt_only":
        return "response_absent_candidate"
    return None


def _presence_metrics(
    dataset: VisualEvaluationDataset,
    predictions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    eligible = [case for case in dataset.cases if case.manual_answer_presence != "uncertain"]
    true_positive = false_positive = false_negative = true_negative = 0
    abstained_positive = abstained_negative = 0
    for case in eligible:
        predicted = _predicted_presence(predictions[case.case_id])
        truth = case.manual_answer_presence
        if predicted is None:
            if truth == "response_present":
                abstained_positive += 1
            else:
                abstained_negative += 1
        elif predicted == "response_present" and truth == "response_present":
            true_positive += 1
        elif predicted == "response_present":
            false_positive += 1
        elif truth == "response_present":
            false_negative += 1
        else:
            true_negative += 1

    accepted = true_positive + false_positive + false_negative + true_negative
    total = len(dataset.cases)
    model_abstentions = sum(
        _predicted_presence(predictions[case.case_id]) is None for case in dataset.cases
    )
    return {
        "positive_class": "response_present",
        "negative_class": (
            "candidate_prompt_only -> response_absent_candidate (not a human absence decision)"
        ),
        "confusion_counts": {
            "tp": true_positive,
            "fp": false_positive,
            "fn": false_negative,
            "tn": true_negative,
            "abstained_positive_truth": abstained_positive,
            "abstained_negative_truth": abstained_negative,
            "uncertain_manual_truth_excluded": sum(
                case.manual_answer_presence == "uncertain" for case in dataset.cases
            ),
        },
        "precision": _ratio(true_positive, true_positive + false_positive, "没有候选正类预测"),
        "recall": _ratio(true_positive, true_positive + false_negative, "已接受判断中没有正类真值"),
        "false_positive_rate": _ratio(
            false_positive, false_positive + true_negative, "已接受判断中没有负类真值"
        ),
        "false_negative_rate": _ratio(
            false_negative, false_negative + true_positive, "已接受判断中没有正类真值"
        ),
        "abstention_rate": _ratio(model_abstentions, total, "没有评测案例"),
        "automatic_coverage": _ratio(accepted, len(eligible), "没有确定的人工作答存在性标签"),
        "accepted_subset_count": accepted,
        "eligible_case_count": len(eligible),
        "quality_scope": (
            "precision/recall/FPR/FNR use only decisive model outputs "
            "on non-uncertain manual labels"
        ),
    }


def _target_regions(case: VisualEvaluationCase) -> list[Any]:
    return [
        region
        for region in case.ground_truth_regions
        if region.question_number in {None, case.question_number}
    ]


def _candidate_detection_metrics(
    dataset: VisualEvaluationDataset,
    predictions: dict[str, dict[str, Any]],
    threshold: float,
) -> dict[str, Any]:
    """Measure region detection separately from the per-case presence decision."""

    tp = fp = fn = 0
    eligible_count = 0
    matched_ious: list[float] = []
    for case in dataset.cases:
        if case.manual_answer_presence == "uncertain":
            continue
        eligible_count += 1
        truth = [
            region
            for region in case.ground_truth_regions
            if region.role == "response" and region.question_number == case.question_number
        ]
        record = predictions[case.case_id]
        suggestion = record.get("suggestion") if record.get("status") == "ok" else None
        raw_regions = suggestion.get("visual_regions", []) if isinstance(suggestion, dict) else []
        candidates = [
            region
            for region in raw_regions
            if isinstance(region, dict)
            and region.get("evidence_type") != "printed_prompt"
            and isinstance(region.get("page_index"), int)
            and isinstance(region.get("visual_bbox"), (list, tuple))
            and len(region["visual_bbox"]) == 4
        ]
        matches, used_predictions, used_truth = _match_regions(candidates, truth, threshold)
        tp += len(matches)
        fp += len(candidates) - len(used_predictions)
        fn += len(truth) - len(used_truth)
        matched_ious.extend(match[2] for match in matches)

    return {
        "scope": "target-question response regions only; uncertain manual cases excluded",
        "iou_threshold": threshold,
        "counts": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "predicted_candidate_regions": tp + fp,
            "ground_truth_response_regions": tp + fn,
            "eligible_case_count": eligible_count,
            "uncertain_manual_case_count": sum(
                case.manual_answer_presence == "uncertain" for case in dataset.cases
            ),
        },
        "precision": _ratio(tp, tp + fp, "没有预测的 response 区域"),
        "recall": _ratio(tp, tp + fn, "没有目标 Question 的 response 区域"),
        "matched_iou_mean": {
            "status": "evaluated" if matched_ious else "not_evaluated",
            "value": sum(matched_ious) / len(matched_ious) if matched_ious else None,
            "numerator_sum": sum(matched_ious) if matched_ious else None,
            "denominator": len(matched_ious),
            "reason": None if matched_ious else "没有达到IoU阈值的候选匹配",
        },
    }


def _validate_prediction_run(
    dataset: VisualEvaluationDataset,
    prediction_run: dict[str, Any],
    *,
    expected_dataset_sha256: str | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(prediction_run, dict):
        raise ValueError("prediction run must be a JSON object")
    if prediction_run.get("schema_name") != PREDICTION_SCHEMA_NAME:
        raise ValueError("unsupported prediction run schema_name")
    if prediction_run.get("schema_version") != PREDICTION_SCHEMA_VERSION:
        raise ValueError("unsupported prediction run schema_version")
    if prediction_run.get("dataset_id") != dataset.dataset_id:
        raise ValueError("prediction dataset_id does not match the evaluation dataset")
    if prediction_run.get("dataset_version") != dataset.dataset_version:
        raise ValueError("prediction dataset version does not match the evaluation dataset")

    recorded_digest = prediction_run.get("dataset_sha256")
    if not isinstance(recorded_digest, str) or _SHA256_PATTERN.fullmatch(recorded_digest) is None:
        raise ValueError("prediction dataset SHA-256 is missing or invalid")
    if expected_dataset_sha256 is not None:
        if _SHA256_PATTERN.fullmatch(expected_dataset_sha256) is None:
            raise ValueError("expected dataset SHA-256 is invalid")
        if recorded_digest != expected_dataset_sha256:
            raise ValueError("prediction dataset SHA-256 does not match the evaluation dataset")

    output_cases = prediction_run.get("cases")
    if not isinstance(output_cases, list):
        raise ValueError("prediction run must contain a cases list")
    expected = {case.case_id: case for case in dataset.cases}
    by_id: dict[str, dict[str, Any]] = {}
    for record in output_cases:
        if not isinstance(record, dict):
            raise ValueError("every prediction case must be a JSON object")
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("prediction case_id must be a non-empty string")
        if case_id in by_id:
            raise ValueError(f"duplicate case_id in prediction run: {case_id}")
        if case_id not in expected:
            raise ValueError(f"unexpected prediction case_id: {case_id}")
        _validate_prediction_record(expected[case_id], record)
        by_id[case_id] = record

    expected_ids = set(expected)
    if set(by_id) != expected_ids:
        missing = sorted(expected_ids - set(by_id))
        raise ValueError(
            f"prediction case IDs must exactly match the evaluation dataset; missing={missing}"
        )
    return by_id


def _validate_prediction_record(
    case: VisualEvaluationCase,
    record: dict[str, Any],
) -> None:
    status = record.get("status")
    if not isinstance(status, str) or status not in _PREDICTION_STATUSES:
        raise ValueError(f"prediction case {case.case_id} has unknown status")

    elapsed_ms = record.get("elapsed_ms")
    if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int) or elapsed_ms < 0:
        raise ValueError(f"prediction case {case.case_id} has invalid elapsed_ms")
    source_bytes = record.get("source_bytes_total")
    if isinstance(source_bytes, bool) or not isinstance(source_bytes, int) or source_bytes < 0:
        raise ValueError(f"prediction case {case.case_id} has invalid source_bytes_total")

    rendered_pages = record.get("rendered_pages")
    if not isinstance(rendered_pages, list):
        raise ValueError(f"prediction case {case.case_id} must list rendered_pages")
    provided_page_indexes: list[int] = []
    for page in rendered_pages:
        if not isinstance(page, dict):
            raise ValueError(f"prediction case {case.case_id} contains an invalid rendered page")
        page_index = page.get("page_index")
        if isinstance(page_index, bool) or not isinstance(page_index, int):
            raise ValueError(f"prediction case {case.case_id} contains an invalid page_index")
        provided_page_indexes.append(page_index)
        for dimension in ("width", "height"):
            value = page.get(dimension)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(
                    f"prediction case {case.case_id} contains an invalid rendered page {dimension}"
                )

    if len(set(provided_page_indexes)) != len(provided_page_indexes):
        raise ValueError(f"prediction case {case.case_id} has duplicate rendered page indexes")
    expected_page_indexes = {page.page_index for page in case.pages}
    provided_page_set = set(provided_page_indexes)
    if not provided_page_set <= expected_page_indexes:
        raise ValueError(f"prediction case {case.case_id} lists a page not provided for the case")

    usage = record.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise ValueError(f"prediction case {case.case_id} has invalid token usage")
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"prediction case {case.case_id} has invalid {field}")

    if status == "ok":
        if record.get("protocol_valid") is not True:
            raise ValueError(
                f"successful prediction case {case.case_id} is not marked protocol_valid"
            )
        if provided_page_set != expected_page_indexes:
            raise ValueError(
                f"case {case.case_id} does not include every dataset page"
            )
        input_digest = record.get("input_digest")
        if not isinstance(input_digest, str) or _SHA256_PATTERN.fullmatch(input_digest) is None:
            raise ValueError(f"successful prediction case {case.case_id} has invalid input_digest")
        suggestion = record.get("suggestion")
        if not isinstance(suggestion, dict):
            raise ValueError(f"successful prediction case {case.case_id} is missing suggestion")
        try:
            reply = VisualEvidenceModelReply.model_validate(suggestion)
        except ValidationError as error:
            issue_summary = "; ".join(
                f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
                for issue in error.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            )
            raise ValueError(
                f"case {case.case_id} has invalid VisualEvidenceModelReply: "
                f"{issue_summary}"
            ) from error
        if any(region.page_index not in provided_page_set for region in reply.visual_regions):
            raise ValueError(
                f"case {case.case_id} references a page not given to the model"
            )
        return

    if record.get("protocol_valid") is not False:
        raise ValueError(f"failed prediction case {case.case_id} must set protocol_valid to false")
    error_code = record.get("error_code")
    if not isinstance(error_code, str) or not error_code:
        raise ValueError(f"failed prediction case {case.case_id} is missing error_code")
    if record.get("suggestion") is not None:
        raise ValueError(f"failed prediction case {case.case_id} cannot contain a suggestion")


def _region_metrics(
    dataset: VisualEvaluationDataset,
    predictions: dict[str, dict[str, Any]],
    threshold: float,
) -> dict[str, Any]:
    tp = fp = fn = 0
    matched_ious: list[float] = []
    best_iou_for_truth: list[float] = []
    type_errors = 0
    unmatched_prediction_types: Counter[str] = Counter()
    case_details: dict[str, dict[str, Any]] = {}

    for case in dataset.cases:
        truth = _target_regions(case)
        record = predictions[case.case_id]
        suggestion = record.get("suggestion") if record.get("status") == "ok" else None
        predicted = suggestion.get("visual_regions", []) if isinstance(suggestion, dict) else []
        valid_predictions = [
            region
            for region in predicted
            if isinstance(region, dict)
            and isinstance(region.get("page_index"), int)
            and isinstance(region.get("visual_bbox"), (list, tuple))
            and len(region["visual_bbox"]) == 4
        ]
        matches, used_predictions, used_truth = _match_regions(valid_predictions, truth, threshold)
        for p_index, g_index, iou in matches:
            matched_ious.append(iou)
            if valid_predictions[p_index].get("evidence_type") != truth[g_index].label:
                type_errors += 1

        case_fp = len(valid_predictions) - len(used_predictions)
        case_fn = len(truth) - len(used_truth)
        tp += len(matches)
        fp += case_fp
        fn += case_fn
        for p_index, region in enumerate(valid_predictions):
            if p_index not in used_predictions:
                unmatched_prediction_types[str(region.get("evidence_type", "unknown"))] += 1
        for g_index, item in enumerate(truth):
            best = max(
                (
                    box_iou(
                        tuple(float(point) for point in region["visual_bbox"]),
                        item.bbox,
                    )
                    for region in valid_predictions
                    if region["page_index"] == item.page_index
                ),
                default=0.0,
            )
            best_iou_for_truth.append(best)
        case_details[case.case_id] = {
            "matched_region_count": len(matches),
            "unmatched_prediction_count": case_fp,
            "missing_ground_truth_count": case_fn,
            "region_type_error_count": sum(
                valid_predictions[p_index].get("evidence_type") != truth[g_index].label
                for p_index, g_index, _ in matches
            ),
            "unmatched_prediction_types": sorted(
                str(valid_predictions[index].get("evidence_type", "unknown"))
                for index in range(len(valid_predictions))
                if index not in used_predictions
            ),
        }
    iou_sum = sum(matched_ious)
    best_iou_sum = sum(best_iou_for_truth)
    return {
        "iou_threshold": threshold,
        "bbox_iou_mean_on_matches": {
            "status": "evaluated" if matched_ious else "not_evaluated",
            "value": iou_sum / len(matched_ious) if matched_ious else None,
            "numerator_sum": iou_sum if matched_ious else None,
            "denominator": len(matched_ious),
            "reason": None if matched_ious else "没有达到指定IoU阈值的匹配区域",
        },
        "best_iou_mean_per_ground_truth": {
            "status": "evaluated" if best_iou_for_truth else "not_evaluated",
            "value": best_iou_sum / len(best_iou_for_truth) if best_iou_for_truth else None,
            "numerator_sum": best_iou_sum if best_iou_for_truth else None,
            "denominator": len(best_iou_for_truth),
            "reason": None if best_iou_for_truth else "没有目标人工区域",
        },
        "localization_precision": _ratio(tp, tp + fp, "没有模型预测区域"),
        "localization_recall": _ratio(tp, tp + fn, "没有目标人工区域"),
        "counts": {
            "matched_regions_tp": tp,
            "unmatched_predictions_fp": fp,
            "missing_ground_truth_fn": fn,
            "unmatched_prediction_count": fp,
            "region_type_error_count": type_errors,
            "unmatched_prediction_types": dict(sorted(unmatched_prediction_types.items())),
        },
        "case_details": case_details,
        "matching_method": (
            "per-case deterministic global IoU-descending greedy one-to-one matching"
        ),
    }


def _match_regions(
    predictions: list[dict[str, Any]],
    truth: list[Any],
    threshold: float,
) -> tuple[list[tuple[int, int, float]], set[int], set[int]]:
    edges = [
        (
            box_iou(
                tuple(float(point) for point in predictions[p_index]["visual_bbox"]),
                item.bbox,
            ),
            p_index,
            g_index,
        )
        for p_index in range(len(predictions))
        for g_index, item in enumerate(truth)
        if predictions[p_index]["page_index"] == item.page_index
    ]
    edges = sorted((iou, p_index, g_index) for iou, p_index, g_index in edges if iou >= threshold)
    edges.sort(key=lambda edge: (-edge[0], edge[1], edge[2]))
    used_predictions: set[int] = set()
    used_truth: set[int] = set()
    matches: list[tuple[int, int, float]] = []
    for iou, p_index, g_index in edges:
        if p_index in used_predictions or g_index in used_truth:
            continue
        used_predictions.add(p_index)
        used_truth.add(g_index)
        matches.append((p_index, g_index, iou))
    return matches, used_predictions, used_truth


def _quality_metrics(
    dataset: VisualEvaluationDataset,
    predictions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    records = [predictions[case.case_id] for case in dataset.cases]
    latencies = sorted(
        int(record["elapsed_ms"])
        for record in records
        if isinstance(record.get("elapsed_ms"), int) and record["elapsed_ms"] >= 0
    )
    prompt_tokens = [
        int(record["usage"]["prompt_tokens"])
        for record in records
        if isinstance(record.get("usage"), dict)
        and isinstance(record["usage"].get("prompt_tokens"), int)
    ]
    completion_tokens = [
        int(record["usage"]["completion_tokens"])
        for record in records
        if isinstance(record.get("usage"), dict)
        and isinstance(record["usage"].get("completion_tokens"), int)
    ]
    successful = sum(record.get("status") == "ok" for record in records)
    protocol_failures = sum(record.get("status") == "protocol_error" for record in records)
    request_failures = sum(record.get("status") == "request_error" for record in records)
    input_failures = sum(record.get("status") == "input_error" for record in records)

    def percentile(values: list[int], fraction: float) -> int | None:
        if not values:
            return None
        return values[max(0, math.ceil(fraction * len(values)) - 1)]

    return {
        "json_protocol_pass_rate": _ratio(successful, len(records), "没有模型请求结果"),
        "model_request_failure_rate": _ratio(request_failures, len(records), "没有模型请求"),
        "protocol_failure_count": protocol_failures,
        "request_failure_count": request_failures,
        "input_failure_count": input_failures,
        "total_failure_count": protocol_failures + request_failures + input_failures,
        "request_count": len(records),
        "elapsed_ms": {
            "mean": sum(latencies) / len(latencies) if latencies else None,
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "observed_count": len(latencies),
        },
        "token_usage": {
            "prompt_tokens_sum": sum(prompt_tokens) if prompt_tokens else None,
            "completion_tokens_sum": sum(completion_tokens) if completion_tokens else None,
            "observed_count": min(len(prompt_tokens), len(completion_tokens)),
        },
        "source_image_count": sum(len(case.pages) for case in dataset.cases),
        "source_bytes_total": sum(int(record.get("source_bytes_total", 0)) for record in records),
        "rendered_image_count": sum(len(record.get("rendered_pages", [])) for record in records),
        "rendered_pixels_total": sum(
            int(page.get("width", 0)) * int(page.get("height", 0))
            for record in records
            for page in record.get("rendered_pages", [])
        ),
    }


def _case_failures(
    dataset: VisualEvaluationDataset,
    predictions: dict[str, dict[str, Any]],
    region_details: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for case in dataset.cases:
        record = predictions[case.case_id]
        categories: list[str] = []
        if record.get("status") == "protocol_error":
            categories.append("model_protocol_error")
        elif record.get("status") == "request_error":
            categories.append("provider_request_error")
        elif record.get("status") == "input_error":
            categories.append("render_or_input_error")
        else:
            predicted = _predicted_presence(record)
            if predicted is None and case.manual_answer_presence != "uncertain":
                categories.append("model_abstention")
            elif (
                case.manual_answer_presence != "uncertain"
                and predicted != case.manual_answer_presence
                and not (
                    predicted == "response_absent_candidate"
                    and case.manual_answer_presence == "response_absent"
                )
            ):
                categories.append(
                    "visual_model_presence_mismatch"
                    if case.manual_answer_presence == "response_present"
                    else "possible_visual_false_positive"
                )
        region = region_details[case.case_id]
        if region["missing_ground_truth_count"]:
            categories.append("visual_region_not_detected")
        if region["unmatched_prediction_count"]:
            categories.append("unmatched_visual_region")
        if region["region_type_error_count"]:
            categories.append("visual_region_type_mismatch")
        if categories:
            failures.append(
                {
                    "case_id": case.case_id,
                    "categories": categories,
                    "status": record.get("status"),
                    "error_code": record.get("error_code"),
                    "model_outcome": (
                        record.get("suggestion", {}).get("outcome")
                        if isinstance(record.get("suggestion"), dict)
                        else None
                    ),
                    "region_details": region,
                }
            )
    return failures
