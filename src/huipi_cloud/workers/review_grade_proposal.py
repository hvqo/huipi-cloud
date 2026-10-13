"""Controlled local CLI for append-only grading proposal review."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError

from huipi_cloud.infrastructure.database.session import (
    dispose_database_engine,
    session_factory,
)
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.grading.errors import GradeInputError
from huipi_cloud.modules.grading.grade_review_errors import GradeReviewError
from huipi_cloud.modules.grading.grade_review_protocol import (
    GradeReviewDecisionCreate,
    GradeReviewRecordSummary,
)
from huipi_cloud.modules.grading.grade_review_service import (
    grade_review_history,
    inspect_grade_proposal,
    record_grade_review,
)
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError

logger = logging.getLogger(__name__)
MAX_REVIEW_JSON_BYTES = 64 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本地候选评分人工复核工具；不会发布正式成绩或触发模型评分。"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="检查候选分数与来源状态")
    inspect.add_argument("--proposal-id", required=True, type=UUID)

    record = commands.add_parser("record", help="追加一条人工复核记录")
    record.add_argument("--proposal-id", required=True, type=UUID)
    record.add_argument("--request-id", required=True, type=UUID)
    record.add_argument("--expected-revision", required=True, type=int)
    record.add_argument("--file", required=True, type=Path)

    history = commands.add_parser("history", help="查看候选评分的复核修订历史")
    history.add_argument("--proposal-id", required=True, type=UUID)
    return parser


def _write(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _load_review_file(path: Path) -> GradeReviewDecisionCreate:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise GradeReviewError("review_file_unavailable") from error
    if len(payload) > MAX_REVIEW_JSON_BYTES:
        raise GradeReviewError("review_file_too_large")
    try:
        return GradeReviewDecisionCreate.model_validate_json(payload)
    except ValidationError as error:
        raise GradeReviewError("review_protocol_invalid") from error


async def _run(arguments: argparse.Namespace, *, factory=None, storage=None) -> int:
    selected_factory = factory if factory is not None else session_factory
    if selected_factory is None:
        _failure("source_unavailable", "database_not_configured", 1)
        return 1
    try:
        selected_storage = storage if storage is not None else get_object_storage()
        if arguments.command == "inspect":
            result = await inspect_grade_proposal(
                selected_factory,
                selected_storage,
                proposal_id=arguments.proposal_id,
            )
            _write(result.model_dump(mode="json"))
            return 0
        if arguments.command == "record":
            payload = _load_review_file(arguments.file)
            result = await record_grade_review(
                selected_factory,
                selected_storage,
                proposal_id=arguments.proposal_id,
                request_id=arguments.request_id,
                expected_revision=arguments.expected_revision,
                payload=payload,
            )
            review = result.review
            safe = GradeReviewRecordSummary(
                review_id=review.review_id,
                proposal_id=review.proposal_id,
                review_revision=review.review_revision,
                supersedes_review_id=review.supersedes_review_id,
                review_status=review.review_status,
                proposed_total_score=review.proposed_total_score,
                reviewed_total_score=review.reviewed_total_score,
                changed_criterion_count=sum(item.changed for item in review.criterion_reviews),
                created=result.created,
            )
            _write(safe.model_dump(mode="json"))
            return 0
        result = await grade_review_history(
            selected_factory,
            selected_storage,
            proposal_id=arguments.proposal_id,
        )
        _write([item.model_dump(mode="json") for item in result])
        return 0
    except GradeReviewError as error:
        _failure(error.status, error.reason_code, 2)
        return 2
    except GradeProposalError as error:
        _failure(error.status, error.reason_code, 2)
        return 2
    except GradeInputError:
        _failure("source_stale", "grade_input_invalid", 2)
        return 2
    except StorageUnavailableError:
        _failure("source_unavailable", "proposal_storage_unavailable", 1)
        return 1
    except DBAPIError as error:
        logger.error("GradeReview database operation failed (%s)", type(error).__name__)
        _failure("source_unavailable", "database_unavailable", 1)
        return 1
    except HTTPException:
        _failure("source_unavailable", "storage_not_configured", 1)
        return 1
    except Exception as error:
        logger.error("GradeReview command failed (%s)", type(error).__name__)
        _failure("internal_error", "grade_review_failed", 1)
        return 1


def _failure(status: str, reason_code: str, exit_code: int) -> int:
    _write({"status": status, "reason_codes": [reason_code]})
    return exit_code


async def _run_and_dispose(arguments: argparse.Namespace) -> int:
    try:
        return await _run(arguments)
    finally:
        await dispose_database_engine()


def main() -> None:
    arguments = _parser().parse_args()
    sys.exit(asyncio.run(_run_and_dispose(arguments)))


if __name__ == "__main__":
    main()
