"""Controlled CLI for aligning one parsed Submission with its Assignment Questions."""

import argparse
import asyncio
import json
import logging
import sys
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from huipi_cloud.infrastructure.database.session import (
    dispose_database_engine,
    session_factory,
)
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.answer_alignment.errors import (
    AnswerAlignmentArtifactCorruptError,
    AnswerAlignmentLimitError,
    AnswerAlignmentNotFoundError,
    AnswerAlignmentNotReadyError,
)
from huipi_cloud.modules.answer_alignment.service import align_submission

logger = logging.getLogger(__name__)


async def _run(submission_id: UUID) -> int:
    try:
        return await _run_operation(submission_id)
    finally:
        await dispose_database_engine()


async def _run_operation(submission_id: UUID) -> int:
    if session_factory is None:
        _write_result("failed", "database_not_configured")
        return 1
    try:
        storage = get_object_storage()
        record = await align_submission(session_factory, storage, submission_id)
    except AnswerAlignmentNotFoundError:
        _write_result("failed", "submission_not_found")
        return 1
    except AnswerAlignmentNotReadyError:
        _write_result("failed", "canonical_not_ready")
        return 1
    except AnswerAlignmentLimitError as error:
        _write_result("failed", error.args[0] if error.args else "alignment_limit_exceeded")
        return 1
    except AnswerAlignmentArtifactCorruptError:
        _write_result("failed", "canonical_or_alignment_artifact_invalid")
        return 1
    except StorageUnavailableError:
        _write_result("failed", "storage_unavailable")
        return 1
    except DBAPIError as error:
        logger.error("Answer Alignment database operation failed (%s)", type(error).__name__)
        _write_result("failed", "database_unavailable")
        return 1
    except HTTPException:
        _write_result("failed", "object_store_not_configured")
        return 1

    _write_result(
        record.status,
        None,
        submission_id=str(submission_id),
        alignment_id=str(record.id),
        question_count=record.question_count,
        aligned_count=record.aligned_count,
        review_required_count=record.review_required_count,
        unmatched_count=record.unmatched_count,
        not_observed_count=record.not_observed_count,
    )
    return 0


def _write_result(status: str, code: str | None, **fields: object) -> None:
    print(json.dumps({"status": status, "failure_code": code, **fields}, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="对齐一个提交中的学生答案与作业题目")
    parser.add_argument("--submission-id", required=True, type=UUID)
    arguments = parser.parse_args()
    return asyncio.run(_run(arguments.submission_id))


if __name__ == "__main__":
    sys.exit(main())
