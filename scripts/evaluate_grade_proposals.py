#!/usr/bin/env python3
"""Validate synthetic grading references, run opted-in models, or re-score predictions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from huipi_cloud.core.config import Settings  # noqa: E402
from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider  # noqa: E402
from huipi_cloud.infrastructure.llm.deepseek import is_deepseek_endpoint  # noqa: E402
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
from huipi_cloud.modules.grading.proposal_service import (  # noqa: E402
    _PROMPT_TOKEN_WRAPPER_ALLOWANCE,
    _check_prompt_budget,
)
from huipi_cloud.modules.grading.protocol import GradeInputDocument  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "data/evaluation/grading/rubric-cases-v2.json"


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
    parser.add_argument("--max-requests", type=int, default=25)
    parser.add_argument("--timeout-seconds", type=float, help="默认使用环境配置的超时")
    parser.add_argument("--max-output-tokens", type=int, help="默认使用环境配置的输出上限")
    parser.add_argument("--max-estimated-cost-usd", default="0.50")
    parser.add_argument("--input-usd-per-million", help="当前未缓存输入单价，必须自行核对")
    parser.add_argument("--output-usd-per-million", help="当前输出单价，含思考Token")
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
        "configured_provider": config.grading_provider,
        "configured_model": config.grading_model,
        "actual_model_id": None,
        "thinking_mode": (
            config.grading_deepseek_thinking
            if is_deepseek_endpoint(config.grading_base_url) else None
        ),
        "configured_reasoning_effort": (
            config.grading_deepseek_reasoning_effort
            if is_deepseek_endpoint(config.grading_base_url) else None
        ),
        "effective_reasoning_effort": (
            config.grading_deepseek_reasoning_effort
            if is_deepseek_endpoint(config.grading_base_url)
            and config.grading_deepseek_thinking == "enabled" else None
        ),
    }


async def _run_model(
    args: argparse.Namespace,
) -> tuple[RubricEvaluationPredictions, dict[str, object]]:
    dataset, digest = load_dataset(args.dataset)
    included = [case for case in dataset.cases if case.reference_status == "included"]
    if not 1 <= args.max_requests <= 30:
        raise ValueError("max_requests_must_be_between_1_and_30")
    if args.timeout_seconds is not None and not 1 <= args.timeout_seconds <= 180:
        raise ValueError("timeout_seconds_must_be_between_1_and_180")
    if not args.output:
        raise ValueError("--run-model requires --output to preserve its predictions")
    if args.output.exists() or (args.report_output and args.report_output.exists()):
        raise ValueError("model_run_must_not_overwrite_existing_predictions_or_reports")

    base_config = Settings()
    updates: dict[str, object] = {
        "grading_enabled": True,
        "grading_invalid_response_retries": 0,
    }
    if args.timeout_seconds is not None:
        updates["grading_timeout_seconds"] = args.timeout_seconds
    if args.max_output_tokens is not None:
        updates["grading_max_output_tokens"] = args.max_output_tokens
    if args.model:
        updates["grading_model"] = args.model
    config = base_config.model_copy(update=updates)
    to_request = included[: min(args.max_requests, len(included))]
    cost_preflight = _cost_preflight(to_request, config, args)
    provider = build_text_grading_provider(config)
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
    if cost_preflight is not None:
        report["cost_estimate"] = cost_preflight
        usage = report["runtime_usage"]
        if (
            usage["prompt_token_count"] == len(to_request)
            and usage["completion_token_count"] == len(to_request)
        ):
            report["cost_estimate"]["reported_token_estimate_usd"] = str(
                (
                    Decimal(usage["prompt_tokens_total"]) * Decimal(args.input_usd_per_million)
                    + Decimal(usage["completion_tokens_total"])
                    * Decimal(args.output_usd_per_million)
                ) / Decimal(1_000_000)
            )
        else:
            report["cost_estimate"]["reported_token_estimate_usd"] = None
            report["cost_estimate"]["usage_incomplete"] = True
    return predictions, report


def _cost_preflight(cases, config: Settings, args: argparse.Namespace) -> dict | None:
    if not is_deepseek_endpoint(config.grading_base_url):
        return None
    try:
        input_rate = Decimal(args.input_usd_per_million)
        output_rate = Decimal(args.output_usd_per_million)
        budget = Decimal(args.max_estimated_cost_usd)
    except (TypeError, ValueError, InvalidOperation) as error:
        raise ValueError("deepseek_requires_current_prices_and_cost_budget") from error
    if any(not value.is_finite() or value <= 0 for value in (input_rate, output_rate, budget)):
        raise ValueError("deepseek_prices_and_budget_must_be_finite_positive")
    input_bound = 0
    for case in cases:
        user = build_user_prompt(build_grade_input(case))
        _check_prompt_budget(SYSTEM_PROMPT, user, config)
        input_bound += (
            len(SYSTEM_PROMPT.encode()) + len(user.encode()) + _PROMPT_TOKEN_WRAPPER_ALLOWANCE
        )
    output_bound = len(cases) * config.grading_max_output_tokens
    estimate = (input_bound * input_rate + output_bound * output_rate) / Decimal(1_000_000)
    if estimate > budget:
        raise ValueError("deepseek_estimated_run_cost_exceeds_budget")
    return {
        "request_cap": len(cases),
        "input_token_budget_bound": input_bound,
        "output_token_cap": output_bound,
        "input_usd_per_million": str(input_rate),
        "output_usd_per_million": str(output_rate),
        "preflight_estimate_usd": str(estimate),
        "budget_usd": str(budget),
        "definition": (
            "无格式重试；UTF-8字节与协议开销作保守输入预算，输出含思考；"
            "按用户核对的未缓存单价估算，不是账单或账户级硬限额。"
        ),
    }


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.max_requests < 1 or args.max_requests > 30:
        parser.error("--max-requests 必须在1到30之间")
    if args.max_output_tokens is not None and not 64 <= args.max_output_tokens <= 16384:
        parser.error("--max-output-tokens 必须在64到16384之间")
    if sum((args.run_model, args.evaluate_predictions is not None, args.dry_run)) > 1:
        parser.error("--run-model、--evaluate-predictions 和 --dry-run 只能选一个")
    try:
        if args.report_output and args.report_output.exists():
            raise ValueError("report_output_must_be_new_to_preserve_history")
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
        reason = (
            error.reason_code if isinstance(error, GradeProposalError)
            else "prediction_validation_failed" if isinstance(error, ValidationError)
            else str(error)
        )
        print(f"评测失败：{reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
