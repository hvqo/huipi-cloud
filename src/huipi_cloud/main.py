"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

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
