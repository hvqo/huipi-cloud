#!/usr/bin/env python3
"""Validate synthetic grading references, run opted-in models, or re-score predictions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from huipi_cloud.core.config import Settings  # noqa: E402
from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider  # noqa: E402
from huipi_cloud.modules.grading.evaluation import (  # noqa: E402
    EvaluationPredictionCase,
    RubricEvaluationPredictions,
    build_grade_input,
    create_predictions_document,
    evaluate_predictions,
    load_dataset,
    request_prediction,
)
from huipi_cloud.modules.grading.prompts import (  # noqa: E402
    SYSTEM_PROMPT,
    build_user_prompt,
)
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError  # noqa: E402
from huipi_cloud.modules.grading.proposal_service import _check_prompt_budget  # noqa: E402
from huipi_cloud.modules.grading.protocol import GradeInputDocument  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "data/evaluation/grading/rubric-cases-v1.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "离线评估 Rubric 候选评分。默认只校验合成数据，不调用模型。"
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--dry-run", action="store_true", help="只校验标签并统计输入规模")
    parser.add_argument("--run-model", action="store_true", help="显式允许发送合成输入到Provider")
    parser.add_argument("--evaluate-predictions", type=Path)
    parser.add_argument("--output", type=Path, help="保存真实Provider预测JSON")
    parser.add_argument("--report-output", type=Path, help="保存独立指标JSON报告")
    parser.add_argument("--model", help="本地已安装模型名称，例如 qwen3.5:4b")
    parser.add_argument("--max-requests", type=int, default=24)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    return parser


def _read_predictions(path: Path) -> RubricEvaluationPredictions:
    return RubricEvaluationPredictions.model_validate_json(path.read_bytes())


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def _json_print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _validate_dry_run(dataset_path: Path) -> dict[str, object]:
    dataset, digest = load_dataset(dataset_path)
    included = [case for case in dataset.cases if case.reference_status == "included"]
    excluded = [case for case in dataset.cases if case.reference_status != "included"]
    config = Settings()
    prompt_bytes: list[int] = []
    for case in included:
        document: GradeInputDocument = build_grade_input(case)
        prompt = build_user_prompt(document)
        _check_prompt_budget(SYSTEM_PROMPT, prompt, config)
        prompt_bytes.append(len(prompt.encode("utf-8")) + len(SYSTEM_PROMPT.encode("utf-8")))
        if hasattr(document, "reference_total_score"):
            raise AssertionError("reference labels must not enter GradeInputDocument")
    return {
        "mode": "dry_run",
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": digest,
        "case_count": len(dataset.cases),
        "included_reference_count": len(included),
        "excluded_uncertain_count": len(excluded),
        "reference_source": dataset.source,
        "prompt_count": len(prompt_bytes),
        "max_prompt_bytes": max(prompt_bytes, default=0),
        "provider_requests_sent": 0,
    }


async def _run_model(
    args: argparse.Namespace,
) -> tuple[RubricEvaluationPredictions, dict[str, object]]:
    dataset, digest = load_dataset(args.dataset)
    included = [case for case in dataset.cases if case.reference_status == "included"]
    if not 1 <= args.max_requests <= 30:
        raise ValueError("max_requests_must_be_between_1_and_30")
    if not 1 <= args.timeout_seconds <= 180:
        raise ValueError("timeout_seconds_must_be_between_1_and_180")
    if not args.output:
        raise ValueError("--run-model requires --output to preserve its predictions")

    base_config = Settings()
    updates: dict[str, object] = {
        "grading_enabled": True,
        "grading_invalid_response_retries": 0,
        "grading_timeout_seconds": args.timeout_seconds,
    }
    if args.model:
        updates["grading_model"] = args.model
    config = base_config.model_copy(update=updates)
    provider = build_text_grading_provider(config)
    to_request = included[: min(args.max_requests, len(included))]
    requested_ids = {case.case_id for case in to_request}
    prediction_cases: list[EvaluationPredictionCase] = []
    for index, case in enumerate(dataset.cases, start=1):
        if case.reference_status == "excluded_uncertain":
            prediction = EvaluationPredictionCase(
                case_id=case.case_id,
                status="excluded_uncertain",
            )
        elif case.case_id not in requested_ids:
            prediction = EvaluationPredictionCase(case_id=case.case_id, status="not_requested")
        else:
            prediction = await request_prediction(case, provider, config)
            print(
                f"[{len(prediction_cases) + 1}/{len(dataset.cases)}] "
                f"{case.case_id}: {prediction.status}",
                file=sys.stderr,
                flush=True,
            )
        prediction_cases.append(prediction)

    predictions = create_predictions_document(
        dataset,
        digest,
        provider,
        config,
        prediction_cases,
    )
    report = evaluate_predictions(dataset, digest, predictions, config=config)
    return predictions, report


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.max_requests < 1 or args.max_requests > 30:
        parser.error("--max-requests 必须在1到30之间")
    if sum((args.run_model, args.evaluate_predictions is not None, args.dry_run)) > 1:
        parser.error("--run-model、--evaluate-predictions 和 --dry-run 只能选一个")
    try:
        if args.evaluate_predictions:
            dataset, digest = load_dataset(args.dataset)
            predictions = _read_predictions(args.evaluate_predictions)
            report = evaluate_predictions(dataset, digest, predictions)
            if args.report_output:
                _write_json(args.report_output, report)
            _json_print(report)
            return 0
        if args.run_model:
            predictions, report = asyncio.run(_run_model(args))
            _write_json(args.output, predictions.model_dump(mode="json"))
            if args.report_output:
                _write_json(args.report_output, report)
            _json_print(report)
            return 0
        report = _validate_dry_run(args.dataset)
        if args.report_output:
            _write_json(args.report_output, report)
        _json_print(report)
        return 0
    except (OSError, ValueError, ValidationError, GradeProposalError) as error:
        reason = error.reason_code if isinstance(error, GradeProposalError) else str(error)
        print(f"评测失败：{reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
