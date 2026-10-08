"""Version 1 API router."""

from fastapi import APIRouter

from huipi_cloud.api.v1.health import router as health_router
from huipi_cloud.modules.assignments.router import (
    assignment_router,
    question_router,
)
from huipi_cloud.modules.submissions.router import (
    assignment_submission_router,
    submission_router,
)

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(assignment_router)
api_router.include_router(question_router)
api_router.include_router(assignment_submission_router)
api_router.include_router(submission_router)
