"""Version 1 API router."""

from fastapi import APIRouter, Depends

from huipi_cloud.api.v1.health import router as health_router
from huipi_cloud.modules.answer_alignment.router import router as answer_alignment_router
from huipi_cloud.modules.answer_review.router import router as answer_review_router
from huipi_cloud.modules.assignments.router import (
    assignment_router,
    question_router,
)
from huipi_cloud.modules.canonical_documents.router import router as canonical_document_router
from huipi_cloud.modules.grading.grade_review_router import router as grade_review_router
from huipi_cloud.modules.identity.dependencies import authorize_request
from huipi_cloud.modules.identity.router import router as identity_router
from huipi_cloud.modules.parsing.router import router as parsing_router
from huipi_cloud.modules.submissions.router import (
    assignment_submission_router,
    submission_router,
)

api_router = APIRouter()
protected_router = APIRouter(
    dependencies=[Depends(authorize_request)],
    responses={401: {"description": "缺少或无效教师Token"},
               403: {"description": "无权执行操作"},
               404: {"description": "资源不存在或没有作业授权"},
               503: {"description": "认证或授权服务不可用，拒绝访问"}},
)
api_router.include_router(health_router)
protected_router.include_router(assignment_router)
protected_router.include_router(question_router)
protected_router.include_router(assignment_submission_router)
protected_router.include_router(submission_router)
protected_router.include_router(parsing_router)
protected_router.include_router(canonical_document_router)
protected_router.include_router(answer_alignment_router)

protected_router.include_router(identity_router)
protected_router.include_router(answer_review_router)
protected_router.include_router(grade_review_router)
api_router.include_router(protected_router)
