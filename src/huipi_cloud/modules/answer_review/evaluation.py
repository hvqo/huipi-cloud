"""Validate the synthetic manual baseline and report separate detector metrics."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from huipi_cloud.modules.answer_review.annotations import AnswerPresenceAnnotationDataset

DEFAULT_DATASET = (
    Path(__file__).resolve().parents[4] / "tests/fixtures/answer_presence_synthetic_v1.json"
)


def _ratio(numerator: int, denominator: int, reason: str) -> dict[str, object]:
    return {
        "status": "evaluated" if denominator else "not_evaluated",
        "value": numerator / denominator if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "reason": None if denominator else reason,
    }


def evaluate_predictions(
    dataset: AnswerPresenceAnnotationDataset, predictions: dict[str, str] | None
):
    if predictions is None:
        unavailable = {
            "status": "not_evaluated",
            "value": None,
            "numerator": None,
            "denominator": None,
            "reason": "当前没有自动作答存在性检测器或预测文件",
        }
        return {
            "precision": unavailable,
            "recall": unavailable,
            "false_positive_rate": unavailable,
            "confusion_counts": None,
        }

    eligible = {
        case.case_id: case.decision
        for case in dataset.cases
        if case.review_status == "reviewed"
        and case.decision in {"response_present", "response_absent"}
    }
    if set(predictions) != set(eligible):
        missing = sorted(set(eligible) - set(predictions))
        extra = sorted(set(predictions) - set(eligible))
        raise ValueError(f"预测案例必须与可评测标签完全匹配; missing={missing}; extra={extra}")
    if any(value not in {"response_present", "response_absent"} for value in predictions.values()):
        raise ValueError("二分类预测必须是response_present或response_absent")

    true_positive = false_positive = false_negative = true_negative = 0
    for case_id, truth in eligible.items():
        predicted = predictions[case_id]
        if truth == "response_present" and predicted == "response_present":
            true_positive += 1
        elif truth == "response_absent" and predicted == "response_present":
            false_positive += 1
        elif truth == "response_present":
            false_negative += 1
        else:
            true_negative += 1
    return {
        "precision": _ratio(true_positive, true_positive + false_positive, "没有正类预测"),
        "recall": _ratio(true_positive, true_positive + false_negative, "没有正类人工真值"),
        "false_positive_rate": _ratio(
            false_positive,
            false_positive + true_negative,
            "没有response_absent人工真值",
        ),
        "confusion_counts": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
        },
    }


def evaluate_dataset(
    dataset: AnswerPresenceAnnotationDataset,
    predictions: dict[str, str] | None = None,
) -> dict[str, object]:
    reviewed = [case for case in dataset.cases if case.review_status == "reviewed"]
    decisions = Counter(case.decision for case in reviewed)
    labels = Counter(region.label for case in dataset.cases for region in case.regions)
    unlinked = sum(
        case.decision == "response_present" and case.question_link == "unlinked"
        for case in reviewed
    )
    return {
        "schema_name": "huipi.answer.presence.evaluation",
        "schema_version": "1.0",
        "dataset": {
            "kind": dataset.dataset_kind,
            "annotation_schema_version": dataset.schema_version,
            "case_count": len(dataset.cases),
            "synthetic_only": True,
        },
        "manual_baseline": {
            "review_coverage": _ratio(len(reviewed), len(dataset.cases), "数据集中没有复核范围"),
            "uncertain_ratio": _ratio(
                decisions["uncertain"],
                len(reviewed),
                "没有已复核范围",
            ),
            "decision_counts": {
                key: decisions[key] for key in ("response_present", "response_absent", "uncertain")
            },
            "unlinked_response_scope_count": unlinked,
            "region_label_counts": dict(sorted(labels.items())),
            "region_consistency": {
                "status": "valid",
                "case_count": len(dataset.cases),
                "region_count": sum(labels.values()),
                "unique_case_ids": True,
                "all_regions_reference_declared_nodes": True,
                "all_text_ranges_use_unicode_codepoint_half_open_offsets": True,
                "overlapping_regions": 0,
            },
        },
        "automatic_detector_metrics": evaluate_predictions(dataset, predictions),
        "metric_definitions": {
            "positive_class": "response_present",
            "negative_class": "response_absent",
            "uncertain_and_unreviewed": "excluded from binary detector denominators",
            "precision": "TP / (TP + FP)",
            "recall": "TP / (TP + FN)",
            "false_positive_rate": "FP / (FP + TN)",
            "manual_labels_are_predictions": False,
        },
        "limitations": [
            "合成数据只验证标注协议和统计逻辑，不代表真实教学作业准确率。",
            "当前没有自动作答存在性检测器；自动Precision、Recall和FPR不作评估。",
            "纯文本合成样本不能验证手写识别能力。",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="校验并统计作答存在性合成标注基线")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--predictions", type=Path)
    arguments = parser.parse_args()
    try:
        raw = json.loads(arguments.dataset.read_text(encoding="utf-8"))
        dataset = AnswerPresenceAnnotationDataset.model_validate(raw)
        predictions = None
        if arguments.predictions is not None:
            predictions = json.loads(arguments.predictions.read_text(encoding="utf-8"))
            if not isinstance(predictions, dict):
                raise ValueError("预测文件必须为case_id到二分类预测的JSON对象")
        print(json.dumps(evaluate_dataset(dataset, predictions), ensure_ascii=False, indent=2))
        return 0
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as error:
        print(json.dumps({"status": "invalid_dataset", "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
