"""Exam-only HTTP contracts; identity and scope always come from server-side state."""

from functools import wraps
from uuid import UUID

from fastapi import APIRouter
from pydantic import ValidationError

from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.answer_review.errors import AnswerReviewRegionInvalidError
from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.canonical_documents.errors import CanonicalNormalizationError
from huipi_cloud.modules.grading.grade_review_errors import GradeReviewError
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.scans.router import ScanRoute
from huipi_cloud.modules.submissions.router import Storage

from . import service
from .schemas import (
    AnswerDecision,
    ExamMarkDetails,
    Intent,
    ScoreRequest,
    ScoreReview,
    VisualScoreRequest,
    VisualScoreReview,
    VisualSelectionRequest,
)

router = APIRouter(tags=["exam-grading"], route_class=ScanRoute)


def safe(function):
    @wraps(function)
    async def handle(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (
            AnswerReviewRegionInvalidError,
            GradeProposalError,
            GradeReviewError,
            ValidationError,
            CanonicalNormalizationError,
        ):
            raise AccessError("exam_mark_evidence_or_score_invalid", 422) from None
        except (StorageUnavailableError, OSError, TimeoutError):
            raise AccessError("exam_mark_storage_unavailable", 503) from None

    return handle


@router.post("/exam-parsing-jobs/{job_id}/grading-workspace", response_model=dict)
@safe
async def initialize_exam_grading(
    job_id: UUID, body: Intent, session: DbSession, principal: AuthorizedPrincipal, storage: Storage
):
    return await service.initialize(session, principal, storage, job_id, body)


@router.get("/exam-parsing-jobs/{job_id}/grading-questions", response_model=dict)
@safe
async def list_exam_grading_questions(
    job_id: UUID, session: DbSession, principal: AuthorizedPrincipal
):
    return await service.list_workspaces(session, principal, job_id)


@router.get("/exam-grading-workspaces/{workspace_id}", response_model=ExamMarkDetails)
@safe
async def inspect_exam_grading(
    workspace_id: UUID, session: DbSession, principal: AuthorizedPrincipal, storage: Storage
):
    return await service.details(session, principal, storage, workspace_id)


@router.post("/exam-grading-workspaces/{workspace_id}/answer-review", response_model=dict)
@safe
async def record_exam_answer_review(
    workspace_id: UUID,
    body: AnswerDecision,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await service.confirm_answer(session, principal, storage, workspace_id, body)


@router.post(
    "/exam-grading-workspaces/{workspace_id}/scoring", response_model=dict, status_code=202
)
@safe
async def request_exam_scoring(
    workspace_id: UUID,
    body: ScoreRequest,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await service.enqueue(session, principal, storage, workspace_id, body)


@router.post("/exam-grading-workspaces/{workspace_id}/score-review", response_model=dict)
@safe
async def record_exam_score_review(
    workspace_id: UUID,
    body: ScoreReview,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    return await service.record_score_review(session, principal, storage, workspace_id, body)


@router.post("/exam-grading-workspaces/{workspace_id}/visual-selection", response_model=dict)
@safe
async def record_exam_visual_selection(
    workspace_id: UUID,
    body: VisualSelectionRequest,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    from .visual_selection import select_regions

    return await select_regions(session, principal, storage, workspace_id, body)


@router.post(
    "/exam-grading-workspaces/{workspace_id}/visual-scoring", response_model=dict, status_code=202
)
@safe
async def request_exam_visual_scoring(
    workspace_id: UUID,
    body: VisualScoreRequest,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    from .visual_scoring import enqueue

    return await enqueue(session, principal, storage, workspace_id, body)


@router.post("/exam-grading-workspaces/{workspace_id}/visual-score-review", response_model=dict)
@safe
async def record_exam_visual_score_review(
    workspace_id: UUID,
    body: VisualScoreReview,
    session: DbSession,
    principal: AuthorizedPrincipal,
    storage: Storage,
):
    from .visual_scoring import record_review

    return await record_review(session, principal, storage, workspace_id, body)
