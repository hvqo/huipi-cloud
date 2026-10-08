"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from huipi_cloud.api.v1.router import api_router
from huipi_cloud.core.config import settings
from huipi_cloud.core.logging import configure_logging
from huipi_cloud.infrastructure.database.session import dispose_database_engine
from huipi_cloud.modules.assignments.errors import (
    AssignmentConflictError,
    AssignmentNotFoundError,
    AssignmentValidationError,
)

configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await dispose_database_engine()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(api_router, prefix=settings.api_v1_prefix)
logger = logging.getLogger(__name__)


@app.exception_handler(AssignmentNotFoundError)
async def assignment_not_found_handler(
    _: Request,
    error: AssignmentNotFoundError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": str(error)},
    )


@app.exception_handler(AssignmentConflictError)
async def assignment_conflict_handler(
    _: Request,
    error: AssignmentConflictError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(error)},
    )


@app.exception_handler(AssignmentValidationError)
async def assignment_validation_handler(
    _: Request,
    error: AssignmentValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": str(error)},
    )


@app.exception_handler(DBAPIError)
async def database_error_handler(_: Request, error: DBAPIError) -> JSONResponse:
    """Return safe, stable HTTP errors for database failures."""
    if isinstance(error, OperationalError):
        response_status = status.HTTP_503_SERVICE_UNAVAILABLE
        detail = "数据库暂时不可用，请稍后重试"
    elif isinstance(error, IntegrityError):
        constraint_name = _constraint_name(error)
        response_status = status.HTTP_409_CONFLICT
        if constraint_name == "uq_questions_assignment_number":
            detail = "该作业已存在相同题号"
        elif constraint_name == "uq_answer_keys_question_id":
            detail = "该题已存在标准答案"
        elif constraint_name == "uq_rubric_criteria_question_order":
            detail = "评分细则顺序不能重复"
        elif constraint_name and constraint_name.startswith("ck_"):
            detail = "数据未通过完整性校验"
            response_status = status.HTTP_422_UNPROCESSABLE_CONTENT
        else:
            detail = "请求与现有数据冲突"
    else:
        response_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = "数据库操作失败，请稍后重试"

    logger.error("Database request failed (%s)", type(error).__name__)
    return JSONResponse(status_code=response_status, content={"detail": detail})


def _constraint_name(error: IntegrityError) -> str | None:
    original = error.orig
    candidates = (original, getattr(original, "__cause__", None))
    for candidate in candidates:
        if candidate is None:
            continue
        diagnostic = getattr(candidate, "diag", None)
        name = getattr(diagnostic, "constraint_name", None) or getattr(
            candidate,
            "constraint_name",
            None,
        )
        if isinstance(name, str):
            return name
    return None
