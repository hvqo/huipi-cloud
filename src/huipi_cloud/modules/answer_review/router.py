"""Authenticated teacher answer-presence workflow. No score review or grade publication."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from huipi_cloud.core.config import settings
from huipi_cloud.modules.answer_alignment import service as alignment_service
from huipi_cloud.modules.answer_alignment.errors import AnswerAlignmentArtifactCorruptError
from huipi_cloud.modules.answer_review import repository, service
from huipi_cloud.modules.answer_review.errors import (
    AnswerReviewQuestionNotFoundError,
    AnswerReviewUnavailableError,
)
from huipi_cloud.modules.answer_review.http_schemas import (
    EvidenceSummary,
    ReviewHistory,
    ReviewInspection,
    ReviewRequest,
    ReviewWriteResult,
    RevisionSummary,
)
from huipi_cloud.modules.answer_review.protocol import ReviewSourceVersion
from huipi_cloud.modules.assignments.models import Question
from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.canonical_documents.router import Storage
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.submissions.models import Submission


class ReviewRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except (DBAPIError, OSError, TimeoutError):
                # Includes lock timeout and unknown COMMIT acknowledgement; never assume rollback.
                raise AnswerReviewUnavailableError from None

        return safe_handler


router = APIRouter(
    prefix="/submissions",
    route_class=ReviewRoute,
    tags=["answer-reviews"],
    responses={
        409: {"description": "来源、预期修订或幂等请求冲突"},
        422: {"description": "请求结构或来源区域不合法"},
    },
)


def summary(record):
    return RevisionSummary(
        decision_id=record.id,
        question_id=record.question_id,
        decision=record.decision,
        revision=record.revision,
        request_id=record.request_id,
        supersedes_decision_id=record.supersedes_decision_id,
        created_at=record.created_at,
        reviewer_ref=record.reviewer_ref,
        reviewer_user_id=record.reviewer_user_id,
        reviewer_kind="authenticated_teacher" if record.reviewer_user_id else "legacy_cli_claim",
        source=ReviewSourceVersion(
            canonical_document_id=record.canonical_document_id,
            canonical_sha256=record.canonical_sha256,
            alignment_artifact_id=record.alignment_artifact_id,
            alignment_sha256=record.alignment_sha256,
            aligner_version=record.aligner_version,
            assignment_questions_digest=record.assignment_questions_digest,
        ),
        region_counts={
            name: getattr(record, name + "_count")
            for name in (
                "response_regions",
                "excluded_prompt_regions",
                "uncertain_regions",
            )
        },
        reason_codes=record.reason_codes,
    )


@router.get("/{submission_id}/answer-reviews", response_model=ReviewHistory)
async def list_answer_reviews(
    submission_id: UUID,
    session: DbSession,
    response: Response,
    question_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    response.headers["Cache-Control"] = "private, no-store"
    if question_id is not None:
        aid = await session.scalar(
            select(Submission.assignment_id).where(Submission.id == submission_id)
        )
        qaid = await session.scalar(
            select(Question.assignment_id).where(Question.id == question_id)
        )
        if qaid is None or aid != qaid:
            raise AnswerReviewQuestionNotFoundError
    rows = await repository.decision_page(
        session,
        submission_id,
        question_id=question_id,
        limit=limit,
        offset=offset,
    )
    return ReviewHistory(
        submission_id=submission_id,
        records=[summary(row) for row in rows[:limit]],
        offset=offset,
        limit=limit,
        has_next=len(rows) > limit,
        next_offset=offset + limit if len(rows) > limit else None,
    )


@router.get(
    "/{submission_id}/answer-reviews/questions/{question_id}", response_model=ReviewInspection
)
async def inspect_answer_review(
    submission_id: UUID,
    question_id: UUID,
    session: DbSession,
    storage: Storage,
    response: Response,
    evidence_limit: Annotated[int, Query(ge=1, le=50)] = 25,
    evidence_offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    response.headers["Cache-Control"] = "private, no-store"
    bundle = await alignment_service.load_verified_alignment_bundle(
        async_sessionmaker(session.bind, expire_on_commit=False),
        storage,
        submission_id,
    )
    answer = next(
        (a for a in bundle.alignment_document.answers if a.question_id == question_id), None
    )
    if answer is None:
        raise AnswerReviewQuestionNotFoundError
    latest = await repository.latest_decision(session, submission_id, question_id)
    matches = latest is not None and service.is_current_for_bundle(latest, bundle)
    # No raw text, image URI, S3 key or AnswerKey in this bounded projection.
    nodes = service._index_canonical_nodes(bundle.canonical_document)
    evidence, size = [], 0
    for region in answer.source_regions[evidence_offset : evidence_offset + evidence_limit]:
        node = nodes.get(region.content_pointer)
        if node is None:
            raise AnswerAlignmentArtifactCorruptError("unknown Canonical evidence node")
        item = EvidenceSummary(
            content_pointer=region.content_pointer,
            page_number=node.page_number,
            source_block_id=region.source_block_id,
            text_start=region.text_start,
            text_end=region.text_end,
            normalized_type=region.normalized_type,
            asset_count=len(region.asset_refs),
            canonical_page_path=(
                f"{settings.api_v1_prefix}/submissions/{submission_id}/canonical-document/pages/{node.page_number}"
            ),
        )
        size += len(item.model_dump_json().encode())
        if size > 48 * 1024:
            break
        evidence.append(item)
    next_offset = evidence_offset + len(evidence)
    truncated = next_offset < len(answer.source_regions)
    return ReviewInspection(
        submission_id=submission_id,
        question_id=question_id,
        source=ReviewSourceVersion.from_bundle(bundle),
        alignment_status=answer.matching_status,
        answer_presence=latest.decision if matches else "unreviewed",
        expected_revision=latest.revision if latest else 0,
        latest=summary(latest) if latest else None,
        latest_source_matches=matches,
        evidence=evidence,
        evidence_offset=evidence_offset,
        evidence_truncated=truncated,
        next_evidence_offset=next_offset if truncated else None,
    )


@router.post(
    "/{submission_id}/answer-reviews/questions/{question_id}",
    response_model=ReviewWriteResult,
    status_code=201,
    responses={200: {"model": ReviewWriteResult, "description": "相同请求幂等重放"}},
)
async def record_answer_review(
    submission_id: UUID,
    question_id: UUID,
    payload: ReviewRequest,
    session: DbSession,
    storage: Storage,
    principal: AuthorizedPrincipal,
    response: Response,
):
    response.headers["Cache-Control"] = "private, no-store"
    record, created = await service.record_decision(
        async_sessionmaker(session.bind, expire_on_commit=False),
        storage,
        submission_id=submission_id,
        question_id=question_id,
        request_id=payload.request_id,
        payload=payload.for_reviewer(principal.user_id),
        principal=principal,
        expected_revision=payload.expected_revision,
        expected_source=payload.expected_source,
    )
    # Read the immutable row to expose its authenticated identity column, not a string heuristic.
    row = await repository.decision_summary(session, record.decision_id)
    response.status_code = 201 if created else 200
    return ReviewWriteResult(created=created, record=summary(row))
