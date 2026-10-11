"""Validate a versioned visual dataset and run the configured P2-D2B provider."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.visual_evidence.provider import build_vision_provider
from huipi_cloud.infrastructure.visual_evidence.render import render_selected_pages
from huipi_cloud.modules.visual_evaluation.metrics import evaluate_visual_predictions
from huipi_cloud.modules.visual_evaluation.protocol import (
    VisualEvaluationDataset,
    load_dataset,
)
from huipi_cloud.modules.visual_evidence.errors import (
    VisualEvidenceError,
    VisualEvidenceProtocolError,
    VisualEvidenceProviderError,
)
from huipi_cloud.modules.visual_evidence.protocol import (
    VISUAL_PREPROCESSING_VERSION,
    VISUAL_PROMPT_VERSION,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "datasets/visual-evidence/synthetic-v1/dataset.json"
DEFAULT_OUTPUT = ROOT / "datasets/visual-evidence/runs/qwen3.5-4b-synthetic-v1.json"


def _local_endpoint(endpoint: str) -> bool:
    host = (urlparse(endpoint).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _selected_config(provider_name: str, *, confirm_external_data: bool):
    update: dict[str, object] = {
        "visual_evidence_enabled": True,
        "visual_evidence_provider": provider_name,
    }
    if confirm_external_data:
        update.update(
            {
                "visual_evidence_allow_remote": True,
                "visual_evidence_external_data_authorized": True,
            }
        )
    return settings.model_copy(update=update)


def _source_totals(dataset: VisualEvaluationDataset, dataset_path: Path) -> dict[str, int]:
    image_bytes = 0
    pixels = 0
    for case in dataset.cases:
        for page in case.pages:
            image_bytes += (dataset_path.parent / page.image_path).stat().st_size
            pixels += page.width * page.height
    return {
        "request_count": len(dataset.cases),
        "image_count": sum(len(case.pages) for case in dataset.cases),
        "image_bytes": image_bytes,
        "image_pixels": pixels,
    }


def _input_digest(case_id: str, question_number: int, question_stem: str, pages) -> str:
    summary = {
        "case_id": case_id,
        "question_number": question_number,
        "question_stem": question_stem,
        "prompt_version": VISUAL_PROMPT_VERSION,
        "preprocessing_version": VISUAL_PREPROCESSING_VERSION,
        "pages": [
            {
                "page_index": page.page_index,
                "sha256": page.sha256,
                "width": page.rendered_width,
                "height": page.rendered_height,
            }
            for page in pages
        ],
    }
    encoded = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _run_model(
    dataset: VisualEvaluationDataset,
    dataset_path: Path,
    provider,
    config,
    *,
    source_totals: dict[str, int],
) -> dict[str, object]:
    case_results = []
    for case in dataset.cases:
        source_bytes = sum(
            (dataset_path.parent / page.image_path).stat().st_size for page in case.pages
        )
        rendered_pages = []
        started = time.perf_counter()
        try:
            for page in case.pages:
                rendered = render_selected_pages(
                    dataset_path.parent / page.image_path,
                    content_type=page.content_type,
                    selected_page_indexes=[0],
                    config=config,
                )[0]
                rendered_pages.append(replace(rendered, page_index=page.page_index))
            digest = _input_digest(
                case.case_id, case.question_number, case.question_stem, rendered_pages
            )
            response = await provider.analyze(
                question_number=case.question_number,
                question_stem=case.question_stem,
                source_regions=[],
                pages=rendered_pages,
            )
            case_results.append(
                {
                    "case_id": case.case_id,
                    "status": "ok",
                    "input_digest": digest,
                    "source_bytes_total": source_bytes,
                    "rendered_pages": [
                        {
                            "page_index": page.page_index,
                            "sha256": page.sha256,
                            "width": page.rendered_width,
                            "height": page.rendered_height,
                            "content_type": page.content_type,
                            "mapping_eligible": page.mapping_eligible,
                            "mapping_reason": page.mapping_reason,
                        }
                        for page in rendered_pages
                    ],
                    "suggestion": response.reply.model_dump(mode="json"),
                    "protocol_valid": True,
                    "elapsed_ms": response.elapsed_ms,
                    "usage": (
                        response.usage.model_dump(mode="json")
                        if response.usage is not None
                        else None
                    ),
                    "source_mapping": {
                        "status": "not_evaluated",
                        "reason": "no Canonical or AnswerAlignment context was supplied",
                    },
                }
            )
        except VisualEvidenceProtocolError as error:
            case_results.append(
                {
                    "case_id": case.case_id,
                    "status": "protocol_error",
                    "protocol_valid": False,
                    "error_code": error.code,
                    "elapsed_ms": max(0, round((time.perf_counter() - started) * 1000)),
                    "source_bytes_total": source_bytes,
                    "rendered_pages": [
                        {
                            "page_index": page.page_index,
                            "sha256": page.sha256,
                            "width": page.rendered_width,
                            "height": page.rendered_height,
                            "content_type": page.content_type,
                        }
                        for page in rendered_pages
                    ],
                    "source_mapping": {
                        "status": "not_evaluated",
                        "reason": "no Canonical or AnswerAlignment context was supplied",
                    },
                }
            )
        except VisualEvidenceError as error:
            failure_status = (
                "request_error" if isinstance(error, VisualEvidenceProviderError) else "input_error"
            )
            case_results.append(
                {
                    "case_id": case.case_id,
                    "status": failure_status,
                    "protocol_valid": False,
                    "error_code": error.code,
                    "elapsed_ms": max(0, round((time.perf_counter() - started) * 1000)),
                    "source_bytes_total": source_bytes,
                    "rendered_pages": [
                        {
                            "page_index": page.page_index,
                            "sha256": page.sha256,
                            "width": page.rendered_width,
                            "height": page.rendered_height,
                            "content_type": page.content_type,
                        }
                        for page in rendered_pages
                    ],
                    "source_mapping": {
                        "status": "not_evaluated",
                        "reason": "no Canonical or AnswerAlignment context was supplied",
                    },
                }
            )

    return {
        "schema_name": "huipi.visual.evidence.predictions",
        "schema_version": "1.0",
        "run_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "provider": provider.__class__.__name__,
        "model_id": provider.model_id,
        "endpoint_host": urlparse(config.visual_evidence_base_url).hostname,
        "prompt_version": VISUAL_PROMPT_VERSION,
        "preprocessing_version": VISUAL_PREPROCESSING_VERSION,
        "request_budget": source_totals["request_count"],
        "input_summary": source_totals,
        "source_mapping_status": "not_evaluated_without_canonical_alignment_context",
        "cases": case_results,
    }


def _write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def _main_async(arguments: argparse.Namespace) -> int:
    dataset_path = arguments.dataset.resolve()
    dataset = load_dataset(dataset_path)
    totals = _source_totals(dataset, dataset_path)
    if len(dataset.cases) > arguments.max_requests:
        raise ValueError(
            f"dataset has {len(dataset.cases)} cases but request budget is {arguments.max_requests}"
        )
    if totals["image_bytes"] > arguments.max_source_bytes:
        raise ValueError("dataset source image bytes exceed --max-source-bytes")
    if totals["image_pixels"] > arguments.max_total_pixels:
        raise ValueError("dataset source pixels exceed --max-total-pixels")
    if dataset.dataset_kind == "authorized_real" and not arguments.confirm_authorized_real_data:
        raise ValueError("authorized-real datasets require --confirm-authorized-real-data")

    if arguments.evaluate_predictions is not None:
        prediction_path = arguments.evaluate_predictions.resolve()
        prediction_run = json.loads(prediction_path.read_text(encoding="utf-8"))
        expected_digest = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
        if (
            prediction_run.get("dataset_id") != dataset.dataset_id
            or prediction_run.get("dataset_version") != dataset.dataset_version
            or prediction_run.get("dataset_sha256") != expected_digest
        ):
            raise ValueError("stored predictions do not match this dataset version and digest")
        metrics = evaluate_visual_predictions(
            dataset,
            prediction_run,
            iou_threshold=arguments.iou_threshold,
        )
        metrics_path = arguments.output.with_name(
            f"{arguments.output.stem}.metrics{arguments.output.suffix or '.json'}"
        )
        _write_json(metrics_path, metrics)
        print(
            "VISUAL_EVALUATION_RESULT="
            + json.dumps(
                {
                    "prediction_input": str(prediction_path),
                    "metrics_output": str(metrics_path),
                    "answer_presence": metrics["answer_presence"],
                    "candidate_detection": metrics["candidate_detection"],
                    "visual_regions": {
                        key: value
                        for key, value in metrics["visual_regions"].items()
                        if key != "case_details"
                    },
                    "candidate_question_link": metrics["candidate_question_link"],
                    "automatic_accept": metrics["automatic_accept"],
                    "run_quality": metrics["run_quality"],
                    "failure_count": len(metrics["failures"]),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    config = _selected_config(
        arguments.provider,
        confirm_external_data=arguments.confirm_external_data,
    )
    if not _local_endpoint(config.visual_evidence_base_url) and not arguments.confirm_external_data:
        raise ValueError(
            "non-local model endpoint requires explicit --confirm-external-data authorization"
        )
    provider = build_vision_provider(config)
    plan = {
        "mode": "model_run" if arguments.run_model else "dry_run",
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.dataset_version,
        "dataset_kind": dataset.dataset_kind,
        "case_count": len(dataset.cases),
        "source_totals": totals,
        "request_budget": arguments.max_requests,
        "provider": provider.__class__.__name__,
        "model_id": provider.model_id,
        "endpoint_host": urlparse(config.visual_evidence_base_url).hostname,
        "external_data_authorized": bool(arguments.confirm_external_data),
        "authorization_note": (
            "Images will be sent only to the configured endpoint when --run-model is used."
        ),
    }
    print("VISUAL_EVALUATION_PLAN=" + json.dumps(plan, ensure_ascii=False, sort_keys=True))
    if not arguments.run_model:
        return 0

    prediction_run = await _run_model(
        dataset,
        dataset_path,
        provider,
        config,
        source_totals=totals,
    )
    metrics = evaluate_visual_predictions(
        dataset,
        prediction_run,
        iou_threshold=arguments.iou_threshold,
    )
    _write_json(arguments.output, prediction_run)
    metrics_path = arguments.output.with_name(
        f"{arguments.output.stem}.metrics{arguments.output.suffix or '.json'}"
    )
    _write_json(metrics_path, metrics)
    print(
        "VISUAL_EVALUATION_RESULT="
        + json.dumps(
            {
                "prediction_output": str(arguments.output),
                "metrics_output": str(metrics_path),
                "protocol_pass_rate": metrics["run_quality"]["json_protocol_pass_rate"],
                "presence_metrics": metrics["answer_presence"],
                "region_metrics": {
                    key: value
                    for key, value in metrics["visual_regions"].items()
                    if key != "case_details"
                },
                "source_attribution": metrics["question_source_attribution"],
                "latency": metrics["run_quality"]["elapsed_ms"],
                "token_usage": metrics["run_quality"]["token_usage"],
                "failure_count": len(metrics["failures"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a visual-evidence dataset; model calls require --run-model."
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--provider", choices=("ollama_native", "openai_compatible"), default="ollama_native"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--evaluate-predictions",
        type=Path,
        help="recompute metrics from an existing sanitized prediction JSON without model calls",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="validate and print the request plan"
    )
    parser.add_argument(
        "--run-model", action="store_true", help="explicitly call the configured real provider"
    )
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--max-source-bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument("--max-total-pixels", type=int, default=100_000_000)
    parser.add_argument("--iou-threshold", type=float, default=0.5)
    parser.add_argument("--confirm-external-data", action="store_true")
    parser.add_argument("--confirm-authorized-real-data", action="store_true")
    arguments = parser.parse_args()
    if (
        arguments.max_requests < 1
        or arguments.max_source_bytes < 1
        or arguments.max_total_pixels < 1
    ):
        parser.error("request, byte, and pixel budgets must be positive")
    if not math.isfinite(arguments.iou_threshold) or not 0 < arguments.iou_threshold <= 1:
        parser.error("--iou-threshold must be finite and in (0, 1]")
    if arguments.run_model and arguments.dry_run:
        parser.error("--dry-run and --run-model cannot be used together")
    if arguments.evaluate_predictions is not None and (arguments.run_model or arguments.dry_run):
        parser.error("--evaluate-predictions cannot be combined with --run-model or --dry-run")
    try:
        return asyncio.run(_main_async(arguments))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(
            json.dumps(
                {"status": "invalid_evaluation_setup", "error": str(error)}, ensure_ascii=False
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
