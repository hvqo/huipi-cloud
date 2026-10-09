"""Version 1 API router."""

from fastapi import APIRouter

from huipi_cloud.api.v1.health import router as health_router
from huipi_cloud.modules.answer_alignment.router import router as answer_alignment_router
from huipi_cloud.modules.assignments.router import (
    assignment_router,
    question_router,
)
from huipi_cloud.modules.canonical_documents.router import router as canonical_document_router
from huipi_cloud.modules.parsing.router import router as parsing_router
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
api_router.include_router(parsing_router)
api_router.include_router(canonical_document_router)
api_router.include_router(answer_alignment_router)
