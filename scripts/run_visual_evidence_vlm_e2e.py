"""Call the configured real Vision model with generated, fictional image samples."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

# Keep the documented `python scripts/...py` invocation able to import local
# generated fixtures, even when Python only adds the scripts directory to sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.visual_evidence.provider import build_vision_provider
from huipi_cloud.infrastructure.visual_evidence.render import render_selected_pages
from huipi_cloud.modules.visual_evidence.errors import (
    VisualEvidenceError,
    VisualEvidenceProtocolError,
)
from tests.visual_samples import create_visual_samples


async def _run() -> int:
    try:
        provider = build_vision_provider(settings)
    except VisualEvidenceError as error:
        print(json.dumps({"status": "not_run", "failure_code": error.code}))
        return 2

    results = []
    with tempfile.TemporaryDirectory(prefix="huipi-synthetic-vlm-") as directory:
        for name, path in create_visual_samples(Path(directory)).items():
            started = time.perf_counter()
            try:
                pages = render_selected_pages(
                    path,
                    content_type="image/png",
                    selected_page_indexes=[0],
                    config=settings,
                )
                response = await provider.analyze(
                    question_number=1,
                    question_stem="Solve 2 + 2. Show your work.",
                    source_regions=[],
                    pages=pages,
                )
                results.append(
                    {
                        "sample": name,
                        "protocol_valid": True,
                        "outcome": response.reply.outcome,
                        "region_count": len(response.reply.visual_regions),
                        "evidence_types": [
                            region.evidence_type for region in response.reply.visual_regions
                        ],
                        "reason_codes": sorted(
                            {
                                *response.reply.reason_codes,
                                *(
                                    code
                                    for region in response.reply.visual_regions
                                    for code in region.reason_codes
                                ),
                            }
                        ),
                        "elapsed_ms": response.elapsed_ms,
                        "usage": (
                            response.usage.model_dump(mode="json")
                            if response.usage is not None
                            else None
                        ),
                        "canonical_mapping": "not_tested_without_canonical_context",
                    }
                )
            except VisualEvidenceProtocolError as error:
                results.append(
                    {
                        "sample": name,
                        "protocol_valid": False,
                        "failure_kind": "protocol_validation",
                        "failure_code": error.code,
                        "elapsed_ms": max(0, round((time.perf_counter() - started) * 1000)),
                        "canonical_mapping": "not_tested_without_canonical_context",
                    }
                )
            except VisualEvidenceError as error:
                results.append(
                    {
                        "sample": name,
                        "protocol_valid": False,
                        "failure_kind": "request_or_input",
                        "failure_code": error.code,
                        "elapsed_ms": max(0, round((time.perf_counter() - started) * 1000)),
                        "canonical_mapping": "not_tested_without_canonical_context",
                    }
                )
    protocol_valid_count = sum(bool(item["protocol_valid"]) for item in results)
    protocol_validation_failure_count = sum(
        item.get("failure_kind") == "protocol_validation" for item in results
    )
    abstention_count = sum(
        item.get("outcome") == "uncertain" for item in results if item["protocol_valid"]
    )
    print(
        json.dumps(
            {
                "status": "completed" if protocol_valid_count == len(results) else "partial",
                "model_id": provider.model_id,
                "endpoint_host": urlparse(settings.visual_evidence_base_url).hostname,
                "sample_count": len(results),
                "protocol_valid_count": protocol_valid_count,
                "protocol_valid_ratio": protocol_valid_count / len(results) if results else 0.0,
                "structural_validation_failure_count": protocol_validation_failure_count,
                "request_or_input_failure_count": sum(
                    item.get("failure_kind") == "request_or_input" for item in results
                ),
                "source_mapping_tested_count": 0,
                "source_mapping_status": "not_tested_without_canonical_context",
                "abstention_count": abstention_count,
                "abstention_ratio": abstention_count / len(results) if results else 0.0,
                "results": results,
                "evaluation_note": (
                    "Generated fictional samples only. "
                    "This is not a real-student accuracy evaluation."
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if results and all(item["protocol_valid"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
