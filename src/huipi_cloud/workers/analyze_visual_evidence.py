"""Locally controlled CLI for creating one VLM visual evidence proposal."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.infrastructure.visual_evidence.provider import build_vision_provider
from huipi_cloud.modules.visual_evidence.errors import VisualEvidenceError
from huipi_cloud.modules.visual_evidence.service import (
    VisualEvidenceDryRun,
    analyze_question_visual_evidence,
)

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本地生成一条与人工复核分离的视觉作答证据建议"
    )
    parser.add_argument("--submission-id", required=True, type=UUID)
    parser.add_argument("--question-id", required=True, type=UUID)
    parser.add_argument("--request-id", type=UUID, default=uuid4())
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="校验来源、渲染所需页面并报告资源预算，不调用VLM或写入结果",
    )
    return parser


async def _run(arguments: argparse.Namespace) -> int:
    try:
        if session_factory is None:
            _write({"status": "failed", "failure_code": "database_not_configured"})
            return 1
        storage = get_object_storage()
        provider = None if arguments.dry_run else build_vision_provider(settings)
        result = await analyze_question_visual_evidence(
            session_factory,
            storage,
            provider,
            submission_id=arguments.submission_id,
            question_id=arguments.question_id,
            request_id=arguments.request_id,
            dry_run=arguments.dry_run,
        )
        if isinstance(result, VisualEvidenceDryRun):
            _write(
                {
                    "status": "dry_run_ready",
                    "submission_id": str(result.submission_id),
                    "question_id": str(result.question_id),
                    "selected_page_indexes": result.selected_page_indexes,
                    "original_file_sha256": result.original_file_sha256,
                    "model_input_digest": result.model_input_digest,
                    "total_rendered_bytes": result.total_rendered_bytes,
                    "rendered_pages": [
                        page.model_dump(mode="json") for page in result.rendered_pages
                    ],
                }
            )
        else:
            _write(
                {
                    "status": "proposal_saved",
                    "proposal_id": str(result.id),
                    "request_id": str(result.request_id),
                    "submission_id": str(result.submission_id),
                    "question_id": str(result.question_id),
                    "outcome": result.outcome,
                    "model_id": result.model_id,
                    "model_input_digest": result.model_input_digest,
                    "human_review_updated": False,
                }
            )
        return 0
    except VisualEvidenceError as error:
        _write({"status": "failed", "failure_code": error.code})
        return 1
    except StorageUnavailableError:
        _write({"status": "failed", "failure_code": "object_storage_unavailable"})
        return 1
    except DBAPIError as error:
        logger.error("Visual evidence database operation failed (%s)", type(error).__name__)
        _write({"status": "failed", "failure_code": "database_unavailable"})
        return 1
    except HTTPException:
        _write({"status": "failed", "failure_code": "object_storage_not_configured"})
        return 1
    finally:
        await dispose_database_engine()


def _write(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main() -> int:
    arguments = _parser().parse_args()
    return asyncio.run(_run(arguments))


if __name__ == "__main__":
    sys.exit(main())
