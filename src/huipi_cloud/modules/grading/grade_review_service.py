"""Versioned human review workflow for immutable rubric score proposals."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.core.config import Settings, settings
from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage, StorageUnavailableError
from huipi_cloud.modules.grading import grade_review_repository as repository
from huipi_cloud.modules.grading.errors import GradeInputSourceChangedError
from huipi_cloud.modules.grading.grade_review_errors import (
    GradeReviewArtifactCorruptError,
    GradeReviewConflictError,
    GradeReviewIdempotencyConflictError,
    GradeReviewInvalidDecisionError,
    GradeReviewNotFoundError,
    GradeReviewSourceStaleError,
)
from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision
from huipi_cloud.modules.grading.grade_review_protocol import (
    GradeReviewCriterionInput,
    GradeReviewCriterionReview,
    GradeReviewCriterionSummary,
    GradeReviewDecisionCreate,
    GradeReviewHistoryCriterion,
    GradeReviewHistoryItem,
    GradeReviewInspection,
    GradeReviewRecordResult,
    GradeReviewRevisionDocument,
    grade_review_request_sha256,
)
from huipi_cloud.modules.grading.proposal_errors import (
    GradeProposalArtifactCorruptError,
    GradeProposalInputStaleError,
    GradeProposalInvalidEvidenceError,
    GradeProposalNotFoundError,
)
from huipi_cloud.modules.grading.proposal_protocol import (
    GradeProposalDocument,
    NoEvidenceReference,
    QuoteEvidenceReference,
)
from huipi_cloud.modules.grading.proposal_service import (
    _validate_quote_reference,
    read_grade_proposal_artifact,
)
from huipi_cloud.modules.grading.protocol import canonical_json_bytes
from huipi_cloud.modules.grading.service import read_grade_input_artifact
from huipi_cloud.modules.identity.protocol import Principal

SessionFactory = async_sessionmaker[AsyncSession]


async def inspect_grade_proposal(
    session_factory: SessionFactory,
    storage: S3ObjectStorage,
    *,
    proposal_id: UUID,
    config: Settings = settings,
) -> GradeReviewInspection:
    async with session_factory() as session:
        indexed = await repository.get_proposal(session, proposal_id)
    if indexed is None:
        raise GradeReviewNotFoundError("proposal_not_found")

    async with session_factory() as session:
        latest = (
            await session.execute(
                select(GradeReviewRevision.review_revision, GradeReviewRevision.review_status)
                .where(GradeReviewRevision.proposal_id == proposal_id)
                .order_by(GradeReviewRevision.review_revision.desc())
                .limit(1)
            )
        ).first()
    source_status = "current"
    proposal_sha: str | None = None
    proposal: GradeProposalDocument | None = None
    grade_input = None
    try:
        proposal, proposal_sha = await read_grade_proposal_artifact(
            session_factory,
            storage,
            proposal_id,
            config=config,
        )
        grade_input = await read_grade_input_artifact(
            session_factory,
            storage,
            proposal.grade_input_artifact_id,
            config=config,
        )
        _validate_proposal_binding(proposal, grade_input, config)
    except (GradeProposalInputStaleError, GradeInputSourceChangedError):
        source_status = "source_stale"
    except GradeReviewSourceStaleError:
        source_status = "source_stale"
    except (
        StorageUnavailableError,
        GradeProposalArtifactCorruptError,
        GradeReviewArtifactCorruptError,
    ):
        source_status = "unavailable"
    except GradeProposalNotFoundError as error:
        raise GradeReviewNotFoundError(error.reason_code) from error

    criterion_summaries = []
    if source_status == "current" and proposal is not None:
        criterion_summaries = [
            GradeReviewCriterionSummary(
                criterion_id=item.criterion_id,
                proposed_points=item.awarded_points,
                max_points=item.max_points,
            )
            for item in proposal.criterion_scores
        ]
    return GradeReviewInspection(
        proposal_id=proposal_id,
        grading_status=indexed.grading_status,
        source_status=source_status,
        grade_input_artifact_id=indexed.grade_input_artifact_id,
        grade_input_sha256=indexed.grade_input_sha256,
        proposal_sha256=proposal_sha if source_status == "current" else None,
        submission_id=indexed.submission_id,
        question_id=indexed.question_id,
        proposed_total_score=(
            proposal.total_score if source_status == "current" and proposal is not None else None
        ),
        max_score=(grade_input.question.max_score if grade_input is not None else None),
        criterion_scores=criterion_summaries,
        latest_review_revision=0 if latest is None else latest.review_revision,
        latest_review_status=None if latest is None else latest.review_status,
    )


async def record_grade_review(
    session_factory: SessionFactory,
    storage: S3ObjectStorage,
    *,
    proposal_id: UUID,
    request_id: UUID,
    expected_revision: int,
    payload: GradeReviewDecisionCreate,
    principal: Principal | None = None,
    config: Settings = settings,
) -> GradeReviewRecordResult:
    if principal is not None and payload.reviewer_ref != f"teacher_{principal.user_id.hex}":
        raise GradeReviewInvalidDecisionError("authenticated_reviewer_mismatch")
    if expected_revision < 0:
        raise GradeReviewConflictError("expected_revision_must_be_nonnegative")
    request_sha256 = grade_review_request_sha256(
        proposal_id=proposal_id,
        request_id=request_id,
        expected_revision=expected_revision,
        payload=payload,
        reviewer_user_id=principal.user_id if principal else None,
    )

    # Resolve a retry before checking current sources. A committed review remains
    # recoverable after a lost COMMIT acknowledgement or later source change.
    replay = await repository.get_review_by_request(
        session_factory,
        proposal_id=proposal_id,
        request_id=request_id,
        principal=principal,
    )
    if replay is not None:
        if replay.request_sha256 != request_sha256:
            raise GradeReviewIdempotencyConflictError("request_id_payload_conflict")
        return GradeReviewRecordResult(review=_row_to_document(replay), created=False)

    try:
        proposal, proposal_sha256 = await read_grade_proposal_artifact(
            session_factory,
            storage,
            proposal_id,
            config=config,
        )
        grade_input = await read_grade_input_artifact(
            session_factory,
            storage,
            proposal.grade_input_artifact_id,
            config=config,
        )
    except GradeProposalNotFoundError as error:
        raise GradeReviewNotFoundError(error.reason_code) from error
    except GradeProposalInputStaleError as error:
        raise GradeReviewSourceStaleError(error.reason_code) from error
    except GradeInputSourceChangedError as error:
        raise GradeReviewSourceStaleError(error.reason_code) from error
    except GradeProposalArtifactCorruptError as error:
        raise GradeReviewArtifactCorruptError(error.reason_code) from error
    except StorageUnavailableError as error:
        raise GradeReviewArtifactCorruptError("proposal_storage_unavailable") from error

    _validate_proposal_binding(proposal, grade_input, config)
    criteria, proposed_total, reviewed_total = _build_review_criteria(
        proposal,
        grade_input,
        payload,
        config,
    )
    review_draft = GradeReviewRevisionDocument(
        review_id=uuid4(),
        proposal_id=proposal.proposal_id,
        grade_input_artifact_id=proposal.grade_input_artifact_id,
        proposal_sha256=proposal_sha256,
        grade_input_sha256=grade_input.input_sha256,
        submission_id=proposal.submission_id,
        question_id=proposal.question_id,
        review_revision=1,
        supersedes_review_id=None,
        reviewer_ref=payload.reviewer_ref,
        request_id=request_id,
        request_sha256=request_sha256,
        review_status=payload.review_status,
        criterion_reviews=criteria,
        review_reason_codes=list(payload.review_reason_codes),
        review_comment=payload.review_comment,
        proposed_total_score=proposed_total,
        reviewed_total_score=reviewed_total,
        created_at=datetime.now(UTC),
    )
    record, created = await repository.append_review(
        session_factory,
        proposal_id=proposal_id,
        request_id=request_id,
        request_sha256=request_sha256,
        expected_revision=expected_revision,
        grade_input_document=grade_input,
        review_draft=review_draft,
        principal=principal,
    )
    return GradeReviewRecordResult(review=_row_to_document(record), created=created)


async def grade_review_history(
    session_factory: SessionFactory,
    storage: S3ObjectStorage,
    *,
    proposal_id: UUID,
    config: Settings = settings,
) -> list[GradeReviewHistoryItem]:
    async with session_factory() as session:
        indexed = await repository.get_proposal(session, proposal_id)
    if indexed is None:
        raise GradeReviewNotFoundError("proposal_not_found")

    source_status = "current"
    try:
        proposal, _ = await read_grade_proposal_artifact(
            session_factory,
            storage,
            proposal_id,
            config=config,
        )
        grade_input = await read_grade_input_artifact(
            session_factory,
            storage,
            proposal.grade_input_artifact_id,
            config=config,
        )
        _validate_proposal_binding(proposal, grade_input, config)
    except (GradeProposalInputStaleError, GradeInputSourceChangedError):
        source_status = "source_stale"
    except GradeReviewSourceStaleError:
        source_status = "source_stale"
    except (
        StorageUnavailableError,
        GradeProposalArtifactCorruptError,
        GradeReviewArtifactCorruptError,
    ):
        source_status = "unavailable"

    records = await repository.list_reviews(session_factory, proposal_id)
    return [_row_to_history_item(record, source_status=source_status) for record in records]


def _build_review_criteria(
    proposal: GradeProposalDocument,
    grade_input,
    payload: GradeReviewDecisionCreate,
    config: Settings,
    *,
    evidence_validator=None,
    criterion_model=GradeReviewCriterionReview,
) -> tuple[list[GradeReviewCriterionReview], Decimal | None, Decimal | None]:
    validate_evidence = evidence_validator or _validate_score_evidence
    if proposal.grading_status != "completed_candidate":
        if payload.review_status in {"accepted", "revised"}:
            raise GradeReviewInvalidDecisionError("abstained_proposal_cannot_be_scored")
        return [], None, None

    expected = {item.criterion_id: item for item in grade_input.rubric_snapshot}
    original = {item.criterion_id: item for item in proposal.criterion_scores}
    if (
        proposal.max_score != grade_input.question.max_score
        or set(original) != set(expected)
        or any(original[key].max_points != expected[key].points for key in expected)
    ):
        raise GradeReviewArtifactCorruptError("proposal_rubric_snapshot_mismatch")
    for item in proposal.criterion_scores:
        validate_evidence(item.awarded_points, item.evidence_references, grade_input, config)

    proposed_total = proposal.total_score
    if payload.review_status in {"rejected", "regrading_required"}:
        return [], proposed_total, None

    if payload.review_status == "accepted":
        reviews = [
            criterion_model(
                criterion_id=criterion.criterion_id,
                proposed_points=original[criterion.criterion_id].awarded_points,
                reviewed_points=original[criterion.criterion_id].awarded_points,
                max_points=criterion.points,
                rationale=original[criterion.criterion_id].rationale,
                evidence_references=original[criterion.criterion_id].evidence_references,
                changed=False,
            )
            for criterion in grade_input.rubric_snapshot
        ]
        return reviews, proposed_total, proposed_total

    supplied = {item.criterion_id: item for item in payload.criterion_reviews}
    if len(supplied) != len(payload.criterion_reviews) or set(supplied) != set(expected):
        raise GradeReviewInvalidDecisionError("criterion_set_must_match_rubric_exactly")

    reviews: list[GradeReviewCriterionReview] = []
    for criterion in grade_input.rubric_snapshot:
        original_score = original[criterion.criterion_id]
        requested: GradeReviewCriterionInput = supplied[criterion.criterion_id]
        points = requested.reviewed_points
        if points > criterion.points:
            raise GradeReviewInvalidDecisionError("criterion_score_out_of_range")
        changed = points != original_score.awarded_points
        if changed:
            if requested.rationale is None or requested.evidence_references is None:
                raise GradeReviewInvalidDecisionError(
                    "changed_criterion_requires_reason_and_evidence"
                )
            if not requested.evidence_references:
                raise GradeReviewInvalidDecisionError("changed_criterion_requires_evidence")
            rationale = requested.rationale
            evidence_references = requested.evidence_references
        else:
            if requested.rationale is not None or requested.evidence_references is not None:
                raise GradeReviewInvalidDecisionError("unchanged_criterion_must_preserve_proposal")
            rationale = original_score.rationale
            evidence_references = original_score.evidence_references
        validate_evidence(points, evidence_references, grade_input, config)
        reviews.append(
            criterion_model(
                criterion_id=criterion.criterion_id,
                proposed_points=original_score.awarded_points,
                reviewed_points=points,
                max_points=criterion.points,
                rationale=rationale,
                evidence_references=evidence_references,
                changed=changed,
            )
        )
    if not any(item.changed for item in reviews):
        raise GradeReviewInvalidDecisionError("revised_must_change_a_score")
    reviewed_total = sum((item.reviewed_points for item in reviews), Decimal("0.00"))
    if reviewed_total > grade_input.question.max_score:
        raise GradeReviewInvalidDecisionError("question_score_out_of_range")
    return reviews, proposed_total, reviewed_total


def _validate_proposal_binding(proposal, grade_input, config: Settings) -> None:
    if (
        proposal.grade_input_artifact_id != grade_input.artifact_id
        or proposal.grade_input_sha256 != grade_input.input_sha256
        or proposal.submission_id != grade_input.submission_id
        or proposal.question_id != grade_input.question.question_id
    ):
        raise GradeReviewSourceStaleError("proposal_input_binding_changed")
    if proposal.grading_status == "completed_candidate":
        _build_review_criteria_evidence_only(proposal, grade_input, config)


def _build_review_criteria_evidence_only(proposal, grade_input, config: Settings) -> None:
    expected = {item.criterion_id: item for item in grade_input.rubric_snapshot}
    actual = {item.criterion_id: item for item in proposal.criterion_scores}
    if (
        proposal.max_score != grade_input.question.max_score
        or set(actual) != set(expected)
        or any(actual[key].max_points != expected[key].points for key in expected)
    ):
        raise GradeReviewArtifactCorruptError("proposal_rubric_snapshot_mismatch")
    for item in proposal.criterion_scores:
        _validate_score_evidence(item.awarded_points, item.evidence_references, grade_input, config)


def _validate_score_evidence(points, evidence_references, grade_input, config: Settings) -> None:
    has_quote = False
    for evidence in evidence_references:
        if isinstance(evidence, QuoteEvidenceReference):
            try:
                _validate_quote_reference(evidence, grade_input, config)
            except GradeProposalInvalidEvidenceError as error:
                # Keep the underlying stable P3-B code without echoing source text.
                reason_code = getattr(error, "reason_code", "evidence_reference_invalid")
                raise GradeReviewInvalidDecisionError(reason_code) from error
            has_quote = True
        elif not isinstance(evidence, NoEvidenceReference):
            raise GradeReviewInvalidDecisionError("evidence_reference_kind_invalid")
    if points > 0 and not has_quote:
        raise GradeReviewInvalidDecisionError("positive_score_requires_valid_quote")


def _row_to_document(
    record: GradeReviewRevision, *, document_model=GradeReviewRevisionDocument
) -> GradeReviewRevisionDocument:
    try:
        return document_model.model_validate(
            {
                "review_id": record.review_id,
                "proposal_id": record.proposal_id,
                "grade_input_artifact_id": record.grade_input_artifact_id,
                "proposal_sha256": record.proposal_sha256,
                "grade_input_sha256": record.grade_input_sha256,
                "submission_id": record.submission_id,
                "question_id": record.question_id,
                "schema_version": record.review_schema_version,
                "review_revision": record.review_revision,
                "supersedes_review_id": record.supersedes_review_id,
                "reviewer_ref": record.reviewer_ref,
                "request_id": record.request_id,
                "request_sha256": record.request_sha256,
                "review_status": record.review_status,
                "criterion_reviews": record.criterion_reviews,
                "review_reason_codes": record.review_reason_codes,
                "review_comment": record.review_comment,
                "proposed_total_score": record.proposed_total_score,
                "reviewed_total_score": record.reviewed_total_score,
                "created_at": record.created_at,
            }
        )
    except Exception as error:
        raise GradeReviewArtifactCorruptError("review_record_protocol_invalid") from error


def _row_to_history_item(
    record: GradeReviewRevision,
    *,
    source_status: str,
) -> GradeReviewHistoryItem:
    document = _row_to_document(record)
    return GradeReviewHistoryItem(
        review_id=document.review_id,
        proposal_id=document.proposal_id,
        review_revision=document.review_revision,
        supersedes_review_id=document.supersedes_review_id,
        reviewer_ref=document.reviewer_ref,
        request_id=document.request_id,
        request_sha256=document.request_sha256,
        review_status=document.review_status,
        proposed_total_score=document.proposed_total_score,
        reviewed_total_score=document.reviewed_total_score,
        criterion_scores=[
            GradeReviewHistoryCriterion(
                criterion_id=item.criterion_id,
                proposed_points=item.proposed_points,
                reviewed_points=item.reviewed_points,
                max_points=item.max_points,
                changed=item.changed,
            )
            for item in document.criterion_reviews
        ],
        review_reason_codes=document.review_reason_codes,
        review_comment_present=document.review_comment is not None,
        source_status=source_status,
        created_at=document.created_at,
    )


async def review_evidence_locations(
    session_factory: SessionFactory,
    storage: S3ObjectStorage,
    *,
    proposal_id: UUID,
    offset: int,
    limit: int,
    config: Settings = settings,
) -> tuple[list[dict], int]:
    """Verified source coordinates only; never return student quotes or long rationales."""
    proposal, _ = await read_grade_proposal_artifact(
        session_factory,
        storage,
        proposal_id,
        config=config,
    )
    grade_input = await read_grade_input_artifact(
        session_factory,
        storage,
        proposal.grade_input_artifact_id,
        config=config,
    )
    _validate_proposal_binding(proposal, grade_input, config)
    segments = {item.segment_index: item for item in grade_input.selected_answer_segments}
    references = [
        (score.criterion_id, ref)
        for score in proposal.criterion_scores
        for ref in score.evidence_references
        if isinstance(ref, QuoteEvidenceReference)
    ]
    result, size = [], 0
    for criterion_id, ref in references[offset : offset + limit]:
        segment = segments[ref.segment_index]
        item = dict(
            criterion_id=str(criterion_id),
            segment_index=ref.segment_index,
            content_pointer=segment.content_pointer,
            page_number=segment.page_number,
            text_start=ref.text_start,
            text_end=ref.text_end,
            canonical_text_start=segment.text_start + ref.text_start,
            canonical_text_end=segment.text_start + ref.text_end,
            canonical_page_path=(
                f"{config.api_v1_prefix}/submissions/{proposal.submission_id}"
                f"/canonical-document/pages/{segment.page_number}"
            ),
        )
        size += len(canonical_json_bytes(item))
        if size > 48 * 1024:
            break
        result.append(item)
    return result, len(references)
