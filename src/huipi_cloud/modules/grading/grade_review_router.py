"""Authorized candidate review; no model execution and no grade publication."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from fastapi.routing import APIRoute
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.canonical_documents.router import Storage
from huipi_cloud.modules.grading import grade_review_repository as repository
from huipi_cloud.modules.grading import grade_review_service as service
from huipi_cloud.modules.grading.errors import (
    GradeInputArtifactCorruptError,
    GradeInputNotFoundError,
    GradeInputSourceChangedError,
)
from huipi_cloud.modules.grading.grade_review_errors import (
    GradeReviewArtifactCorruptError,
    GradeReviewSourceStaleError,
    GradeReviewUnavailableError,
)
from huipi_cloud.modules.grading.grade_review_http import (
    GradeReviewHTTPHistory,
    GradeReviewHTTPInspection,
    GradeReviewHTTPWriteResult,
    GradeReviewRequest,
    GradeRevisionSummary,
)
from huipi_cloud.modules.grading.proposal_errors import (
    GradeProposalArtifactCorruptError,
    GradeProposalInputStaleError,
    GradeProposalNotFoundError,
)
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal


class GradeReviewRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except (DBAPIError, OSError, TimeoutError):
                raise GradeReviewUnavailableError("grade_review_unavailable") from None
            except (GradeProposalInputStaleError, GradeInputSourceChangedError):
                raise GradeReviewSourceStaleError from None
            except (
                StorageUnavailableError,
                GradeProposalArtifactCorruptError,
                GradeProposalNotFoundError,
                GradeInputArtifactCorruptError,
                GradeInputNotFoundError,
            ):
                raise GradeReviewArtifactCorruptError("grade_input_unavailable") from None

        return safe_handler


router = APIRouter(
    prefix="/submissions/{submission_id}/questions/{question_id}/grade-proposals/{proposal_id}",
    tags=["grade-reviews"],
    route_class=GradeReviewRoute,
    responses={
        409: {"description": "来源、修订或幂等冲突"},
        422: {"description": "评分或证据不合法"},
    },
)


def summary(row):
    return GradeRevisionSummary(
        **dict(row),
        reviewer_kind="authenticated_teacher" if row["reviewer_user_id"] else "legacy_cli_claim",
    )


@router.get("/review", response_model=GradeReviewHTTPInspection)
async def inspect_grade_review(
    submission_id: UUID,
    question_id: UUID,
    proposal_id: UUID,
    session: DbSession,
    storage: Storage,
    response: Response,
    evidence_limit: Annotated[int, Query(ge=0, le=25)] = 0,
    evidence_offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    response.headers["Cache-Control"] = "private, no-store"
    view = await service.inspect_grade_proposal(
        async_sessionmaker(session.bind, expire_on_commit=False),
        storage,
        proposal_id=proposal_id,
    )
    if view.source_status == "unavailable":
        raise GradeReviewUnavailableError("grade_review_unavailable")
    locations, count = [], 0
    if evidence_limit and view.source_status == "current":
        locations, count = await service.review_evidence_locations(
            async_sessionmaker(session.bind, expire_on_commit=False),
            storage,
            proposal_id=proposal_id,
            offset=evidence_offset,
            limit=evidence_limit,
        )
    latest = await repository.review_page(session, proposal_id, limit=1)
    record = summary(latest[0]) if latest else None
    return GradeReviewHTTPInspection(
        **view.model_dump(
            exclude={
                "reviewer_identity_authenticated",
                "latest_review_revision",
                "latest_review_status",
            }
        ),
        latest_review_revision=record.review_revision if record else 0,
        latest_review_status=record.review_status if record else None,
        latest=record,
        reviewer_identity_authenticated=bool(record and record.reviewer_user_id),
        regrading_required=bool(record and record.review_status == "regrading_required"),
        has_review_history=record is not None,
        evidence_locations=locations,
        evidence_offset=evidence_offset,
        evidence_truncated=bool(evidence_limit and evidence_offset + len(locations) < count),
        next_evidence_offset=(
            evidence_offset + len(locations)
            if evidence_limit and evidence_offset + len(locations) < count
            else None
        ),
    )


@router.get("/reviews", response_model=GradeReviewHTTPHistory)
async def list_grade_reviews(
    submission_id: UUID,
    question_id: UUID,
    proposal_id: UUID,
    session: DbSession,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=10)] = 5,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    response.headers["Cache-Control"] = "private, no-store"
    rows = await repository.review_page(session, proposal_id, limit=limit, offset=offset)
    return GradeReviewHTTPHistory(
        proposal_id=proposal_id,
        records=[summary(row) for row in rows[:limit]],
        limit=limit,
        offset=offset,
        has_next=len(rows) > limit,
        next_offset=offset + limit if len(rows) > limit else None,
    )


@router.post(
    "/reviews",
    response_model=GradeReviewHTTPWriteResult,
    status_code=201,
    responses={200: {"model": GradeReviewHTTPWriteResult, "description": "幂等重放"}},
)
async def record_teacher_grade_review(
    submission_id: UUID,
    question_id: UUID,
    proposal_id: UUID,
    payload: GradeReviewRequest,
    session: DbSession,
    storage: Storage,
    principal: AuthorizedPrincipal,
    response: Response,
):
    response.headers["Cache-Control"] = "private, no-store"
    result = await service.record_grade_review(
        async_sessionmaker(session.bind, expire_on_commit=False),
        storage,
        proposal_id=proposal_id,
        request_id=payload.request_id,
        expected_revision=payload.expected_revision,
        payload=payload.for_reviewer(principal.user_id),
        principal=principal,
    )
    record = await repository.review_summary(session, result.review.review_id)
    response.status_code = 201 if result.created else 200
    return GradeReviewHTTPWriteResult(created=result.created, record=summary(record))
