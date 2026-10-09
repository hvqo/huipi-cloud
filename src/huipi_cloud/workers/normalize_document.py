"""Controlled CLI for post-processing one successful MinerU artifact."""

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
from huipi_cloud.modules.canonical_documents.errors import (
    CanonicalDocumentNotFoundError,
    CanonicalNormalizationError,
    CanonicalSourceNotReadyError,
)
from huipi_cloud.modules.canonical_documents.service import normalize_submission

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
        record = await normalize_submission(session_factory, storage, submission_id)
    except CanonicalNormalizationError as error:
        _write_result("failed", error.code)
        return 1
    except CanonicalSourceNotReadyError:
        _write_result("failed", "parsing_not_ready")
        return 1
    except CanonicalDocumentNotFoundError:
        _write_result("failed", "submission_not_found")
        return 1
    except StorageUnavailableError:
        _write_result("failed", "object_store_unavailable")
        return 1
    except DBAPIError as error:
        logger.error("Canonical database operation failed (%s)", type(error).__name__)
        _write_result("failed", "database_unavailable")
        return 1
    except HTTPException:
        _write_result("failed", "object_store_not_configured")
        return 1

    _write_result(
        "available",
        None,
        submission_id=str(submission_id),
        document_id=str(record.id),
        page_count=record.page_count,
        block_count=record.block_count,
        canonical_sha256=record.canonical_sha256,
    )
    return 0


def _write_result(status: str, code: str | None, **fields: object) -> None:
    payload = {"status": status, "failure_code": code, **fields}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="规范化一个已成功解析的作业提交")
    parser.add_argument("--submission-id", required=True, type=UUID)
    arguments = parser.parse_args()
    return asyncio.run(_run(arguments.submission_id))


if __name__ == "__main__":
    sys.exit(main())
