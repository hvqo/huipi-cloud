"""Local-only inspection and preparation CLI for immutable GradeInput packages."""

from __future__ import annotations

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
from huipi_cloud.modules.grading.errors import (
    GradeInputArtifactCorruptError,
    GradeInputCommitOutcomeUnknownError,
    GradeInputIdempotencyConflictError,
    GradeInputNotFoundError,
    GradeInputRegistrationRolledBackError,
    GradeInputSourceChangedError,
)
from huipi_cloud.modules.grading.service import inspect_grade_input, prepare_grade_input

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本地评分输入检查与生成工具；不调用评分模型、不产生分数。"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="检查评分输入准备状态")
    inspect.add_argument("--submission-id", required=True, type=UUID)
    inspect.add_argument("--question-id", required=True, type=UUID)

    prepare = commands.add_parser("prepare", help="生成不可变文本评分输入包")
    prepare.add_argument("--submission-id", required=True, type=UUID)
    prepare.add_argument("--question-id", required=True, type=UUID)
    prepare.add_argument("--request-id", required=True, type=UUID)
    return parser


def _write(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


async def _run(arguments: argparse.Namespace) -> int:
    if session_factory is None:
        _write({"status": "source_unavailable", "reason_codes": ["database_not_configured"]})
        return 1
    try:
        storage = get_object_storage()
        if arguments.command == "inspect":
            result = await inspect_grade_input(
                session_factory,
                storage,
                submission_id=arguments.submission_id,
                question_id=arguments.question_id,
                request_id=UUID(int=0),
            )
            # inspect has no request ID; a semantic digest would include this
            # placeholder and could be mistaken for a prepared package digest.
            _write(result.model_copy(update={"input_sha256": None}).model_dump(mode="json"))
            return 0 if result.status == "ready_text" else 2

        result = await prepare_grade_input(
            session_factory,
            storage,
            submission_id=arguments.submission_id,
            question_id=arguments.question_id,
            request_id=arguments.request_id,
        )
        _write(result.model_dump(mode="json"))
        return 0 if result.artifact_id is not None else 2
    except GradeInputNotFoundError:
        return _failure("submission_or_question_not_found", 1)
    except GradeInputIdempotencyConflictError:
        return _failure("request_id_payload_conflict", 2)
    except GradeInputSourceChangedError:
        return _failure("source_changed_retry_inspect", 2)
    except GradeInputCommitOutcomeUnknownError:
        return _failure("commit_outcome_unknown_retry_same_request_id", 3)
    except GradeInputRegistrationRolledBackError:
        return _failure("database_registration_failed_retry_same_request_id", 1)
    except GradeInputArtifactCorruptError:
        return _failure("stored_grade_input_integrity_failed", 1)
    except StorageUnavailableError:
        return _failure("storage_unavailable", 1)
    except DBAPIError as error:
        logger.error("GradeInput database operation failed (%s)", type(error).__name__)
        return _failure("database_unavailable", 1)
    except HTTPException:
        return _failure("storage_not_configured", 1)


def _failure(reason: str, exit_code: int) -> int:
    _write({"status": "failed", "reason_codes": [reason]})
    return exit_code


def main() -> None:
    arguments = _parser().parse_args()
    exit_code = asyncio.run(_run_and_dispose(arguments))
    sys.exit(exit_code)


async def _run_and_dispose(arguments: argparse.Namespace) -> int:
    try:
        return await _run(arguments)
    finally:
        await dispose_database_engine()


if __name__ == "__main__":
    main()
