#!/usr/bin/env python3
"""Opt-in paired quote experiment; defaults to dataset validation without model calls."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from huipi_cloud.core.config import Settings  # noqa: E402
from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider  # noqa: E402
from huipi_cloud.infrastructure.llm.deepseek import is_deepseek_endpoint  # noqa: E402
from huipi_cloud.modules.grading.evaluation import (  # noqa: E402
    RubricEvaluationPredictions,
    load_dataset,
)
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError  # noqa: E402
from huipi_cloud.modules.grading.proposal_service import (  # noqa: E402
    _PROMPT_TOKEN_WRAPPER_ALLOWANCE,
    _check_prompt_budget,
)
from huipi_cloud.modules.grading.quote_evaluation import (  # noqa: E402
    QuoteExperimentPredictions,
    compare_experiments,
    create_experiment_predictions,
    evaluate_experiment,
    experiment_prompts,
    import_strict_baseline,
    request_experiment_prediction,
    unrequested_prediction,
)

DEFAULT_DATASET = PROJECT_ROOT / "data/evaluation/grading/rubric-cases-v2.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--dry-run", action="store_true")
    actions.add_argument("--run-model", action="store_true")
    actions.add_argument("--evaluate-predictions", type=Path)
    actions.add_argument(
        "--compare-predictions", nargs=2, type=Path, metavar=("STRICT", "SELECTION")
    )
    parser.add_argument(
        "--mode", choices=["strict", "quote_selection", "paired"], default="quote_selection"
    )
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="Run Quote Selection without importing a strict-baseline prediction file.",
    )
    parser.add_argument(
        "--probe-excluded-uncertain",
        action="store_true",
        help="Request excluded_uncertain cases once as abstention probes, outside score metrics.",
    )
    parser.add_argument("--output-dir", type=Path, help="模型运行的全新独立目录，必须不存在")
    parser.add_argument("--report-output", type=Path, help="离线报告的新文件；不覆盖已有结果")
    parser.add_argument("--model", help="使用已配置的Provider模型；不下载模型")
    parser.add_argument("--max-requests", type=int, default=25, help="每组请求上限")
    parser.add_argument("--timeout-seconds", type=float, help="默认使用环境配置")
    parser.add_argument("--max-output-tokens", type=int, help="默认使用环境配置")
    parser.add_argument("--strict-predictions", type=Path, help="不可变A组严格协议预测文件")
    parser.add_argument(
        "--preregistered-manifest",
        type=Path,
        help="选填：把实验前锁定的数据集与准入门槛清单绑定到运行记录。",
    )
    parser.add_argument("--input-usd-per-million", help="DeepSeek未缓存输入单价，须预先核对")
    parser.add_argument("--output-usd-per-million", help="DeepSeek输出单价，须预先核对")
    parser.add_argument("--max-estimated-cost-usd", default="0.50")
    return parser


def _write_checkpoint(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _write_new_report(path: Path, value: object) -> None:
    # Exclusive create also rejects aliases to datasets, predictions or historical reports.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


async def _run_model(args) -> dict:
    if args.mode != "quote_selection":
        raise ValueError("model_run_must_be_quote_selection_only")
    if args.output_dir is None:
        raise ValueError("run_model_requires_new_output_dir")
    if args.report_output is not None:
        raise ValueError("run_model_reports_are_saved_in_output_dir")
    if args.standalone and args.strict_predictions is not None:
        raise ValueError("standalone_run_must_not_import_strict_baseline")
    if not args.standalone and args.strict_predictions is None:
        raise ValueError("run_model_requires_existing_strict_baseline")
    if args.probe_excluded_uncertain and not args.standalone:
        raise ValueError("uncertain_abstention_probes_require_standalone_run")
    if args.output_dir.exists():
        raise ValueError("model_run_output_dir_must_be_new")

    dataset, digest = load_dataset(args.dataset)
    preregistered = None
    if args.preregistered_manifest is not None:
        preregistered_raw = args.preregistered_manifest.read_bytes()
        manifest_digest = hashlib.sha256(preregistered_raw).hexdigest()
        manifest_sidecar = args.preregistered_manifest.with_suffix(
            args.preregistered_manifest.suffix + ".sha256"
        )
        manifest_tokens = manifest_sidecar.read_text(encoding="ascii").strip().split()
        if not manifest_tokens or manifest_tokens[0] != manifest_digest:
            raise ValueError("preregistered_manifest_sha_mismatch")
        preregistered = json.loads(preregistered_raw)
        if (
            preregistered.get("dataset_sha256") != digest
            or not preregistered.get("labels_locked_before_predictions")
            or not preregistered.get("acceptance_criteria_locked_before_model_requests")
        ):
            raise ValueError("preregistered_manifest_does_not_match_locked_dataset")
        criteria_path = args.preregistered_manifest.parent / preregistered.get(
            "acceptance_criteria_file", ""
        )
        criteria_digest = (
            hashlib.sha256(criteria_path.read_bytes()).hexdigest()
            if criteria_path.is_file()
            else None
        )
        if criteria_digest != preregistered.get("acceptance_criteria_sha256"):
            raise ValueError("preregistered_acceptance_criteria_sha_mismatch")
        criteria_sidecar = criteria_path.with_suffix(criteria_path.suffix + ".sha256")
        criteria_tokens = criteria_sidecar.read_text(encoding="ascii").strip().split()
        if not criteria_tokens or criteria_tokens[0] != preregistered.get(
            "acceptance_criteria_sha256"
        ):
            raise ValueError("preregistered_acceptance_criteria_manifest_mismatch")
        preregistered = {
            "manifest_file": args.preregistered_manifest.name,
            "manifest_sha256": manifest_digest,
            "acceptance_criteria_sha256": preregistered["acceptance_criteria_sha256"],
        }
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
    if (
        config.grading_provider != "openai_compatible"
        or not is_deepseek_endpoint(config.grading_base_url)
        or config.grading_model != "deepseek-flash"
        or config.grading_deepseek_thinking != "disabled"
        or config.grading_invalid_response_retries != 0
        or not config.grading_allow_remote
        or not config.grading_external_data_authorized
        or config.grading_api_key is None
    ):
        raise ValueError("deepseek_synthetic_authorization_and_configuration_required")

    strict_raw = args.strict_predictions.read_bytes() if args.strict_predictions else None
    strict_predictions = (
        RubricEvaluationPredictions.model_validate_json(strict_raw) if strict_raw else None
    )
    included = [case for case in dataset.cases if case.reference_status == "included"]
    requested = included[: min(args.max_requests, len(included))]
    if len(requested) != len(included):
        raise ValueError("experiment_requires_all_included_cases")
    abstention_probes = (
        [case for case in dataset.cases if case.reference_status == "excluded_uncertain"]
        if args.probe_excluded_uncertain
        else []
    )
    all_requested = [*requested, *abstention_probes]

    provider = build_text_grading_provider(config)
    strict_document = None
    strict_report = None
    if strict_predictions is not None:
        strict_document = import_strict_baseline(
            dataset, digest, strict_predictions, provider, config
        )
        strict_report = evaluate_experiment(dataset, digest, strict_document)
        strict_requested = {
            prediction.case_id
            for prediction in strict_document.cases
            if prediction.provider_attempted
        }
        if strict_requested != {case.case_id for case in requested}:
            raise ValueError("strict_baseline_requested_case_set_mismatch")

    cost_preflight = _cost_preflight(all_requested, config, args)
    mode = "quote_selection"
    predictions = [unrequested_prediction(case, mode) for case in dataset.cases]
    args.output_dir.mkdir(parents=True, exist_ok=False)

    def snapshot():
        document = create_experiment_predictions(
            dataset, digest, mode, provider, config, predictions
        )
        _write_checkpoint(
            args.output_dir / "quote_selection-predictions.json", document.model_dump(mode="json")
        )
        return document

    if strict_document is not None and strict_predictions is not None and strict_report is not None:
        _write_checkpoint(
            args.output_dir / "strict-predictions-imported.json",
            strict_document.model_dump(mode="json"),
        )
        _write_checkpoint(args.output_dir / "strict-report-revalidated.json", strict_report)
        _write_checkpoint(
            args.output_dir / "strict-predictions-source.json",
            strict_predictions.model_dump(mode="json"),
        )
    _write_checkpoint(
        args.output_dir / "run-manifest.json",
        {
            "dataset_sha256": digest,
            "strict_predictions_sha256": (
                hashlib.sha256(strict_raw).hexdigest() if strict_raw is not None else None
            ),
            "provider": provider.provider_name,
            "configured_model": provider.model_id,
            "thinking_mode": config.grading_deepseek_thinking,
            "timeout_seconds": config.grading_timeout_seconds,
            "max_output_tokens": config.grading_max_output_tokens,
            "invalid_response_retries": 0,
            "request_cap": len(requested),
            "abstention_probe_count": len(abstention_probes),
            "abstention_probe_case_ids": [case.case_id for case in abstention_probes],
            "requested_call_count": len(all_requested),
            "standalone_quote_selection": args.standalone,
            "preregistered": preregistered,
            "cost_preflight": cost_preflight,
            "data_classification": "synthetic_only",
        },
    )
    snapshot()
    probe_predictions = []
    probe_ids = {case.case_id for case in abstention_probes}
    if abstention_probes:
        _write_checkpoint(
            args.output_dir / "abstention-probes.json",
            {
                "schema_name": "huipi.grading.abstention-probes",
                "schema_version": "1.0",
                "dataset_version": dataset.dataset_version,
                "dataset_sha256": digest,
                "model_id": provider.model_id,
                "prompt_version": "rubric-quote-selection-v1",
                "expected_behavior": "abstain",
                "score_metrics_included": False,
                "cases": [],
            },
        )

    request_order = []
    for index, case in enumerate(dataset.cases):
        if case.reference_status == "included":
            prediction = await request_experiment_prediction(case, mode, provider, config)
            predictions[index] = prediction
            request_order.append({"case_id": case.case_id, "mode": mode, "kind": "scored"})
        elif case.case_id in probe_ids:
            prediction = await request_experiment_prediction(case, mode, provider, config)
            probe_predictions.append(prediction)
            request_order.append(
                {
                    "case_id": case.case_id,
                    "mode": mode,
                    "kind": "excluded_uncertain_abstention_probe",
                }
            )
            _write_checkpoint(
                args.output_dir / "abstention-probes.json",
                {
                    "schema_name": "huipi.grading.abstention-probes",
                    "schema_version": "1.0",
                    "dataset_version": dataset.dataset_version,
                    "dataset_sha256": digest,
                    "model_id": provider.model_id,
                    "prompt_version": "rubric-quote-selection-v1",
                    "expected_behavior": "abstain",
                    "score_metrics_included": False,
                    "cases": [item.model_dump(mode="json") for item in probe_predictions],
                },
            )
        else:
            continue

        document = snapshot()
        _write_checkpoint(
            args.output_dir / "quote_selection-report.json",
            evaluate_experiment(dataset, digest, document),
        )
        print(
            f"[{len(request_order)}/{len(all_requested)}] {case.case_id}: "
            f"{prediction.status} ({prediction.elapsed_ms}ms)",
            file=sys.stderr,
            flush=True,
        )

    document = snapshot()
    report = evaluate_experiment(dataset, digest, document)
    all_predictions = [
        item for item in [*predictions, *probe_predictions] if item.provider_attempted
    ]
    usage = {
        "prompt_tokens_total": sum(item.prompt_tokens or 0 for item in all_predictions),
        "completion_tokens_total": sum(item.completion_tokens or 0 for item in all_predictions),
        "prompt_token_count": sum(item.prompt_tokens is not None for item in all_predictions),
        "completion_token_count": sum(
            item.completion_tokens is not None for item in all_predictions
        ),
    }
    report["cost_estimate"] = _actual_cost_estimate(
        {
            "runtime_usage": usage,
            "counts": {"provider_requests_sent": len(all_predictions)},
        },
        cost_preflight,
    )
    if abstention_probes:
        probe_counts = Counter(item.status for item in probe_predictions)
        _write_checkpoint(
            args.output_dir / "abstention-probe-report.json",
            {
                "expected_behavior": "abstain",
                "score_metrics_included": False,
                "probe_cases": len(abstention_probes),
                "provider_requests_sent": sum(
                    item.provider_attempted for item in probe_predictions
                ),
                "abstain_cases": probe_counts.get("abstain", 0),
                "non_abstain_or_failure_cases": len(abstention_probes)
                - probe_counts.get("abstain", 0),
                "status_counts": dict(sorted(probe_counts.items())),
                "cases": [
                    {
                        "case_id": item.case_id,
                        "status": item.status,
                        "error_code": item.error_code,
                        "elapsed_ms": item.elapsed_ms,
                        "response_model_id": item.response_model_id,
                        "prompt_tokens": item.prompt_tokens,
                        "completion_tokens": item.completion_tokens,
                        "finish_reason": item.finish_reason,
                        "human_review_status": "unreviewed",
                    }
                    for item in probe_predictions
                ],
                "raw_responses_file": "abstention-probes.json",
            },
        )
    _write_checkpoint(args.output_dir / "quote_selection-report.json", report)

    paired = None
    if strict_document is not None:
        paired = compare_experiments(dataset, digest, strict_document, document)
        _write_checkpoint(args.output_dir / "paired-report.json", paired)
    _write_checkpoint(
        args.output_dir / "run-order.json",
        {
            "dataset_sha256": digest,
            "ordering": (
                "one request per case; no retries; excluded probes stay outside score metrics"
            ),
            "request_order": request_order,
        },
    )
    groups = {
        "quote_selection": {
            "counts": report["counts"],
            "total_score": report["total_score"],
            "criterion_score": report["criterion_score"],
            "runtime_usage": report["runtime_usage"],
        }
    }
    if strict_report is not None:
        groups["strict"] = {
            "counts": strict_report["counts"],
            "total_score": strict_report["total_score"],
            "criterion_score": strict_report["criterion_score"],
            "runtime_usage": strict_report["runtime_usage"],
        }
    return {
        "dataset_sha256": digest,
        "output_dir": str(args.output_dir),
        "model": provider.model_id,
        "thinking_mode": config.grading_deepseek_thinking,
        "timeout_seconds": config.grading_timeout_seconds,
        "max_output_tokens": config.grading_max_output_tokens,
        "groups": groups,
        "cost_estimate": report["cost_estimate"],
        "paired_outcomes": paired["paired_outcomes"] if paired is not None else None,
    }


def _cost_preflight(cases, config: Settings, args) -> dict:
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
        system, user = experiment_prompts(case, "quote_selection")
        _check_prompt_budget(system, user, config)
        input_bound += (
            len(system.encode("utf-8"))
            + len(user.encode("utf-8"))
            + _PROMPT_TOKEN_WRAPPER_ALLOWANCE
        )
    output_bound = len(cases) * config.grading_max_output_tokens
    estimate = (input_bound * input_rate + output_bound * output_rate) / Decimal(1_000_000)
    if budget > Decimal("0.50"):
        raise ValueError("deepseek_cost_budget_must_not_exceed_0_50_usd")
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
            "UTF-8输入字节和协议开销作为保守上界；未缓存输入价与含思考输出价估算，"
            "不是账单或账户硬限额。"
        ),
    }


def _actual_cost_estimate(report: dict, preflight: dict) -> dict:
    usage = report["runtime_usage"]
    prompt_tokens = usage["prompt_tokens_total"]
    completion_tokens = usage["completion_tokens_total"]
    request_count = report["counts"]["provider_requests_sent"]
    usage_complete = (
        usage["prompt_token_count"] == request_count
        and usage["completion_token_count"] == request_count
    )
    input_rate = Decimal(preflight["input_usd_per_million"])
    output_rate = Decimal(preflight["output_usd_per_million"])
    return {
        **preflight,
        "reported_prompt_tokens": prompt_tokens,
        "reported_completion_tokens": completion_tokens,
        "usage_complete": usage_complete,
        "reported_token_estimate_usd": str(
            (Decimal(prompt_tokens) * input_rate + Decimal(completion_tokens) * output_rate)
            / Decimal(1_000_000)
        ),
        "definition": preflight["definition"] + "按Provider用量估算，非最终账单。",
    }


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 100:
        parser.error("--max-requests must be between 1 and 100")
    if args.timeout_seconds is not None and not 1 <= args.timeout_seconds <= 180:
        parser.error("--timeout-seconds must be between 1 and 180")
    if args.max_output_tokens is not None and not 64 <= args.max_output_tokens <= 4096:
        parser.error("--max-output-tokens must be between 64 and 4096")
    try:
        dataset, digest = load_dataset(args.dataset)
        if args.run_model:
            report = asyncio.run(_run_model(args))
        elif args.evaluate_predictions or args.compare_predictions:
            paths = (
                args.compare_predictions
                if args.compare_predictions
                else [args.evaluate_predictions]
            )
            documents = [
                QuoteExperimentPredictions.model_validate_json(path.read_bytes()) for path in paths
            ]
            report = (
                compare_experiments(dataset, digest, *documents)
                if args.compare_predictions
                else evaluate_experiment(dataset, digest, documents[0])
            )
        else:
            modes = ["strict", "quote_selection"] if args.mode == "paired" else [args.mode]
            config = Settings()
            included_cases = [
                case for case in dataset.cases if case.reference_status == "included"
            ]
            uncertain_cases = (
                [
                    case
                    for case in dataset.cases
                    if case.reference_status == "excluded_uncertain"
                ]
                if args.probe_excluded_uncertain
                else []
            )
            dry_cases = [*included_cases, *uncertain_cases]
            prompt_sizes = {}
            for mode in modes:
                sizes = []
                for case in dry_cases:
                    system, user = experiment_prompts(case, mode)
                    _check_prompt_budget(system, user, config)
                    sizes.append(len(system.encode()) + len(user.encode()))
                prompt_sizes[mode] = max(sizes)
            cost_preflight = None
            if args.input_usd_per_million is not None or args.output_usd_per_million is not None:
                if args.input_usd_per_million is None or args.output_usd_per_million is None:
                    raise ValueError("preflight_requires_both_token_prices")
                cost_preflight = _cost_preflight(dry_cases, config, args)
            report = {
                "mode": "dry_run",
                "dataset_version": dataset.dataset_version,
                "dataset_sha256": digest,
                "dataset_cases": len(dataset.cases),
                "included_cases": len(included_cases),
                "excluded_uncertain_abstention_probes": len(uncertain_cases),
                "planned_provider_requests": len(dry_cases),
                "provider_requests_sent": 0,
                "configured_provider": config.grading_provider,
                "configured_model": config.grading_model,
                "thinking_mode": config.grading_deepseek_thinking,
                "effective_reasoning_effort": (
                    config.grading_deepseek_reasoning_effort
                    if config.grading_deepseek_thinking == "enabled"
                    else None
                ),
                "timeout_seconds": config.grading_timeout_seconds,
                "max_output_tokens": config.grading_max_output_tokens,
                "invalid_response_retries": config.grading_invalid_response_retries,
                "max_prompt_bytes_by_mode": prompt_sizes,
                "cost_preflight": cost_preflight,
            }
        if args.report_output:
            _write_new_report(args.report_output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, ValidationError, GradeProposalError) as error:
        # Do not stringify validation errors, which may contain raw model/student content.
        code = (
            error.reason_code
            if isinstance(error, GradeProposalError)
            else (
                "invalid_experiment_data"
                if isinstance(error, ValidationError)
                else "experiment_io_error"
                if isinstance(error, OSError)
                else str(error)
            )
        )
        print(f"实验失败：{code}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
