"""Local-only inspection and execution CLI for rubric score proposals."""

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
from huipi_cloud.modules.grading.errors import GradeInputError
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError
from huipi_cloud.modules.grading.proposal_service import (
    grade_input_artifact,
    inspect_grade_proposal_input,
)

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本地Rubric候选评分工具；只生成候选评分，不发布最终成绩。"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="检查已验证GradeInput的评分准备状态")
    inspect.add_argument("--grade-input-id", required=True, type=UUID)

    grade = commands.add_parser("grade", help="调用配置的LLM并保存候选评分")
    grade.add_argument("--grade-input-id", required=True, type=UUID)
    grade.add_argument("--request-id", required=True, type=UUID)
    return parser


def _write(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


async def _run(arguments: argparse.Namespace) -> int:
    if session_factory is None:
        _failure("source_unavailable", "database_not_configured", 1)
        return 1
    try:
        storage = get_object_storage()
        if arguments.command == "inspect":
            result = await inspect_grade_proposal_input(
                session_factory,
                storage,
                grade_input_artifact_id=arguments.grade_input_id,
            )
            _write(result.model_dump(mode="json"))
            return 0
        result = await grade_input_artifact(
            session_factory,
            storage,
            grade_input_artifact_id=arguments.grade_input_id,
            request_id=arguments.request_id,
        )
        _write(result.model_dump(mode="json"))
        return 0
    except GradeProposalError as error:
        _failure(error.status, error.reason_code, 2)
        return 2
    except GradeInputError:
        _failure("input_stale", "grade_input_invalid", 2)
        return 2
    except StorageUnavailableError:
        _failure("source_unavailable", "proposal_storage_unavailable", 1)
        return 1
    except DBAPIError as error:
        logger.error("GradeProposal database operation failed (%s)", type(error).__name__)
        _failure("input_stale", "database_unavailable", 1)
        return 1
    except HTTPException:
        _failure("provider_unavailable", "storage_not_configured", 1)
        return 1
    except Exception as error:
        logger.error("GradeProposal command failed (%s)", type(error).__name__)
        _failure("internal_error", "grading_failed", 1)
        return 1


def _failure(status: str, reason_code: str, exit_code: int) -> int:
    _write({"status": status, "reason_codes": [reason_code]})
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
