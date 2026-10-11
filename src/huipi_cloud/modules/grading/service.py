"""Build and verify source-bound, text-ready GradeInput packages."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.core.config import Settings, settings
from huipi_cloud.infrastructure.database.base import utc_now
from huipi_cloud.infrastructure.storage.s3 import (
    S3ObjectStorage,
    StorageUnavailableError,
)
from huipi_cloud.modules.answer_alignment import service as alignment_service
from huipi_cloud.modules.answer_alignment.errors import (
    AnswerAlignmentArtifactCorruptError,
    AnswerAlignmentNotFoundError,
    AnswerAlignmentNotReadyError,
    AnswerAlignmentResultNotFoundError,
)
from huipi_cloud.modules.answer_review import service as review_service
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.answer_review.protocol import (
    ReviewAssetReference,
    ReviewEvidenceRegion,
)
from huipi_cloud.modules.assignments.enums import AssignmentStatus
from huipi_cloud.modules.assignments.models import (
    AnswerKey,
    Assignment,
    RubricCriterion,
)
from huipi_cloud.modules.canonical_documents.protocol import (
    CanonicalBlock,
    CanonicalContentNode,
    CanonicalDocument,
)
from huipi_cloud.modules.grading import repository
from huipi_cloud.modules.grading.errors import (
    GradeInputArtifactCorruptError,
    GradeInputCommitOutcomeUnknownError,
    GradeInputError,
    GradeInputNotFoundError,
    GradeInputSourceChangedError,
)
from huipi_cloud.modules.grading.protocol import (
    AnswerKeySnapshot,
    GradeInputAssetReference,
    GradeInputCore,
    GradeInputDocument,
    GradeInputEvidenceItem,
    GradeInputInspection,
    GradeInputPreparationRead,
    GradeInputQuestionSnapshot,
    RubricCriterionSnapshot,
    RubricInspectionItem,
    SelectedAnswerSegment,
    canonical_json_bytes,
    json_sha256,
    rubric_sha256,
)
from huipi_cloud.modules.submissions.models import SubmissionFile

logger = logging.getLogger(__name__)
_GRADE_INPUT_NAMESPACE = UUID("d554ae75-573c-4d35-a928-7f99f1b9ccea")
_REDACTED_INLINE_IMAGE = "canonical-inline-image:"
_MARKDOWN_IMAGE_REFERENCE = re.compile(
    r"(?<!\\)!\[(?:\\.|[^\]\\])*\]\s*(?:\([^\)\n]+\)|\[[^\]\n]*\])"
)
_EMBEDDED_NON_TEXT_TAG = re.compile(
    r"<\s*(?P<tag>img|picture|svg|table|thead|tbody|tfoot|tr|td|th|caption|colgroup|col|"
    r"video|audio|iframe|object|embed|canvas|math)(?=[\s/>])",
    re.IGNORECASE,
)
_IMAGE_MARKUP_TAGS = {"img", "picture", "svg"}
_TABLE_MARKUP_TAGS = {
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "td",
    "th",
    "caption",
    "colgroup",
    "col",
}
_EMBEDDED_MEDIA_TAGS = {"video", "audio", "iframe", "object", "embed", "canvas"}


@dataclass(frozen=True)
class _NodeLocation:
    page: object
    block: CanonicalBlock
    node: CanonicalContentNode
    traversal_order: int


@dataclass(frozen=True)
class _Assessment:
    inspection: GradeInputInspection
    core: GradeInputCore | None


async def inspect_grade_input(
    session_factory: async_sessionmaker[AsyncSession],
    storage: S3ObjectStorage,
    *,
    submission_id: UUID,
    question_id: UUID,
    request_id: UUID,
    config: Settings = settings,
) -> GradeInputInspection:
    """Inspect readiness without printing or returning student answer text."""

    assessment = await _assess(
        session_factory,
        storage,
        submission_id=submission_id,
        question_id=question_id,
        request_id=request_id,
        config=config,
    )
    return assessment.inspection


async def prepare_grade_input(
    session_factory: async_sessionmaker[AsyncSession],
    storage: S3ObjectStorage,
    *,
    submission_id: UUID,
    question_id: UUID,
    request_id: UUID,
    config: Settings = settings,
) -> GradeInputPreparationRead:
    """Persist a private immutable GradeInput only when readiness is ready_text."""

    assessment = await _assess(
        session_factory,
        storage,
        submission_id=submission_id,
        question_id=question_id,
        request_id=request_id,
        config=config,
    )
    if assessment.core is None:
        return GradeInputPreparationRead(inspection=assessment.inspection)

    core = assessment.core
    input_digest = json_sha256(core)
    artifact_id = uuid5(
        _GRADE_INPUT_NAMESPACE,
        f"{submission_id}:{question_id}:{request_id}",
    )
    existing = await repository.find_current_request(
        session_factory,
        core,
        input_sha256=input_digest,
    )
    if existing is not None:
        await read_grade_input_artifact(
            session_factory,
            storage,
            existing.id,
            config=config,
        )
        return GradeInputPreparationRead(
            inspection=assessment.inspection,
            artifact_id=existing.id,
            input_sha256=existing.input_sha256,
            object_sha256=existing.object_sha256,
            created=False,
        )

    document = GradeInputDocument(
        **core.model_dump(),
        artifact_id=artifact_id,
        input_sha256=input_digest,
        created_at=utc_now(),
    )
    payload = canonical_json_bytes(document)
    if len(payload) > config.grade_input_max_document_bytes:
        inspection = assessment.inspection.model_copy(
            update={"status": "size_limit_exceeded", "reason_codes": ["package_size_limit"]}
        )
        return GradeInputPreparationRead(inspection=inspection)

    object_key = _object_key(storage, submission_id, question_id, request_id)
    object_digest = hashlib.sha256(payload).hexdigest()
    try:
        await storage.upload_fileobj(BytesIO(payload), object_key, "application/json")
    except asyncio.CancelledError:
        # boto3's thread cannot be stopped by asyncio cancellation. Keep its unique
        # attempt key and never race a delete against an upload still in flight.
        raise
    except Exception:
        await _best_effort_delete(storage, object_key, "upload_failed")
        raise

    try:
        record, created = await repository.register_success(
            session_factory,
            core=core,
            input_sha256=input_digest,
            artifact_id=artifact_id,
            bucket=storage.bucket,
            object_key=object_key,
            object_sha256=object_digest,
            object_size_bytes=len(payload),
        )
    except GradeInputCommitOutcomeUnknownError:
        # The immutable object may be referenced by a COMMIT whose response was
        # lost. Preserve it; retrying the same request ID resolves the outcome.
        logger.warning("GradeInput commit result is unknown; preserve attempt object")
        raise
    except GradeInputError:
        # These paths are before COMMIT or have an explicit rollback. The upload
        # completed before registration began, so this unique object is safe to
        # remove. CommitOutcomeUnknown is handled above.
        await _best_effort_delete(storage, object_key, "registration_not_committed")
        raise

    if record.bucket != storage.bucket or record.object_key != object_key:
        # A concurrent request with the same key won after its transaction
        # committed. This request's upload is known to be unreferenced.
        await _best_effort_delete(storage, object_key, "duplicate_request_loser")
    return GradeInputPreparationRead(
        inspection=assessment.inspection,
        artifact_id=record.id,
        input_sha256=record.input_sha256,
        object_sha256=record.object_sha256,
        created=created,
    )


async def read_grade_input_artifact(
    session_factory: async_sessionmaker[AsyncSession],
    storage: S3ObjectStorage,
    artifact_id: UUID,
    *,
    config: Settings = settings,
) -> GradeInputDocument:
    """Read one package, recheck current source versions, byte hash, and protocol."""

    async with session_factory() as session:
        record = await repository.get_artifact(session, artifact_id)
        if record is None:
            raise GradeInputNotFoundError

    current = await _assess(
        session_factory,
        storage,
        submission_id=record.submission_id,
        question_id=record.question_id,
        request_id=record.request_id,
        config=config,
    )
    if current.core is None or json_sha256(current.core) != record.input_sha256:
        raise GradeInputSourceChangedError("grade_input_no_longer_current")
    if record.bucket != storage.bucket:
        raise StorageUnavailableError("grade input bucket does not match configured storage")

    payload = await _read_object(
        storage,
        record.object_key,
        expected_size=record.object_size_bytes,
        expected_sha256=record.object_sha256,
        maximum=config.grade_input_max_document_bytes,
    )
    try:
        document = GradeInputDocument.model_validate_json(payload)
        core = GradeInputCore.model_validate(
            document.model_dump(exclude={"artifact_id", "input_sha256", "created_at"})
        )
    except ValidationError as error:
        raise GradeInputArtifactCorruptError from error
    if (
        document.artifact_id != record.id
        or document.input_sha256 != record.input_sha256
        or document.input_sha256 != json_sha256(core)
        or document.submission_id != record.submission_id
        or document.assignment_id != record.assignment_id
        or document.question.question_id != record.question_id
        or document.canonical_artifact_id != record.canonical_artifact_id
        or document.canonical_sha256 != record.canonical_sha256
        or document.alignment_artifact_id != record.alignment_artifact_id
        or document.alignment_sha256 != record.alignment_sha256
        or document.review_decision_id != record.review_decision_id
        or document.review_revision != record.review_revision
        or document.answer_key.answer_key_id != record.answer_key_id
        or document.answer_key.sha256 != record.answer_key_sha256
        or document.rubric_sha256 != record.rubric_sha256
        or document.schema_version != record.schema_version
    ):
        raise GradeInputArtifactCorruptError
    return document


async def _assess(
    session_factory: async_sessionmaker[AsyncSession],
    storage: S3ObjectStorage,
    *,
    submission_id: UUID,
    question_id: UUID,
    request_id: UUID,
    config: Settings,
) -> _Assessment:
    try:
        bundle = await alignment_service.load_verified_alignment_bundle(
            session_factory,
            storage,
            submission_id,
            config=config,
        )
    except AnswerAlignmentNotFoundError as error:
        raise GradeInputNotFoundError from error
    except AnswerAlignmentArtifactCorruptError:
        return _blocked(submission_id, question_id, "source_stale", "canonical_alignment_invalid")
    except (AnswerAlignmentNotReadyError, AnswerAlignmentResultNotFoundError):
        return _blocked(
            submission_id,
            question_id,
            "source_unavailable",
            "canonical_alignment_not_ready",
        )
    except StorageUnavailableError:
        return _blocked(submission_id, question_id, "source_unavailable", "storage_unavailable")
    except DBAPIError:
        return _blocked(submission_id, question_id, "source_unavailable", "database_unavailable")

    question = next(
        (item for item in bundle.context.questions if item.id == question_id),
        None,
    )
    if question is None:
        raise GradeInputNotFoundError
    if not any(item.question_id == question_id for item in bundle.alignment_document.answers):
        return _blocked(
            submission_id,
            question_id,
            "source_stale",
            "alignment_question_missing",
            assignment_id=bundle.context.assignment_id,
        )

    try:
        async with session_factory() as session:
            assignment = await session.scalar(
                select(Assignment).where(Assignment.id == bundle.context.assignment_id)
            )
            answer_key = await session.scalar(
                select(AnswerKey).where(AnswerKey.question_id == question_id)
            )
            rubric_rows = list(
                (
                    await session.scalars(
                        select(RubricCriterion)
                        .where(RubricCriterion.question_id == question_id)
                        .order_by(RubricCriterion.sort_order, RubricCriterion.id)
                    )
                ).all()
            )
            source_file = await session.scalar(
                select(SubmissionFile).where(SubmissionFile.submission_id == submission_id)
            )
            latest_review = await session.scalar(
                select(AnswerReviewDecision)
                .where(
                    AnswerReviewDecision.submission_id == submission_id,
                    AnswerReviewDecision.question_id == question_id,
                )
                .order_by(AnswerReviewDecision.revision.desc())
                .limit(1)
            )
    except DBAPIError:
        return _blocked(submission_id, question_id, "source_unavailable", "database_unavailable")

    reasons: list[str] = []
    if assignment is None or assignment.id != bundle.context.assignment_id:
        return _blocked(submission_id, question_id, "source_stale", "assignment_missing")
    if assignment.status != AssignmentStatus.PUBLISHED.value:
        reasons.append("assignment_not_published")
    if source_file is None or source_file.sha256 != bundle.canonical_artifact.source_sha256:
        reasons.append("submission_source_checksum_mismatch")

    answer_snapshot: AnswerKeySnapshot | None = None
    if answer_key is None:
        reasons.append("answer_key_missing")
    elif answer_key.source != "teacher":
        reasons.append("answer_key_not_teacher_set")
    elif not answer_key.answer_content.strip():
        reasons.append("answer_key_empty")
    else:
        answer_digest = hashlib.sha256(answer_key.answer_content.encode("utf-8")).hexdigest()
        answer_snapshot = AnswerKeySnapshot(
            answer_key_id=answer_key.id,
            question_id=answer_key.question_id,
            source=answer_key.source,
            answer_content=answer_key.answer_content,
            sha256=answer_digest,
        )

    rubric_snapshot = [
        RubricCriterionSnapshot(
            criterion_id=item.id,
            question_id=item.question_id,
            description=item.description,
            points=item.points,
            sort_order=item.sort_order,
        )
        for item in rubric_rows
    ]
    rubric_digest = rubric_sha256(rubric_snapshot) if rubric_snapshot else None
    rubric_total = sum((item.points for item in rubric_snapshot), start=Decimal("0.00"))
    if not rubric_snapshot:
        reasons.append("rubric_missing")
    elif (
        any(item.points <= 0 or not item.description.strip() for item in rubric_snapshot)
        or len({item.sort_order for item in rubric_snapshot}) != len(rubric_snapshot)
        or rubric_total != question.max_score
    ):
        reasons.append("rubric_total_or_fields_invalid")

    review_read = None
    if latest_review is None:
        reasons.append("human_review_missing")
    else:
        try:
            review_read = review_service.decision_read(latest_review)
        except ValidationError:
            reasons.append("human_review_record_invalid")
        else:
            if not review_service.is_current_for_bundle(review_read, bundle):
                reasons.append("human_review_source_stale")
            if (
                review_read.assignment_id != assignment.id
                or review_read.question_id != question.id
                or review_read.question_number != question.question_number
            ):
                reasons.append("human_review_question_mismatch")
            if review_read.decision != "response_present":
                reasons.append(f"human_review_{review_read.decision}")
            if not review_read.response_regions:
                reasons.append("human_response_regions_missing")

    question_snapshot = GradeInputQuestionSnapshot(
        question_id=question.id,
        question_number=question.question_number,
        question_type=question.question_type,
        stem=question.stem,
        max_score=question.max_score,
    )
    inspection_base = {
        "submission_id": submission_id,
        "assignment_id": assignment.id,
        "question_id": question.id,
        "question_number": question.question_number,
        "question_type": question.question_type,
        "max_score": question.max_score,
        "canonical_artifact_id": bundle.canonical_artifact.id,
        "canonical_sha256": bundle.canonical_artifact.canonical_sha256,
        "alignment_artifact_id": bundle.alignment_artifact.id,
        "alignment_sha256": bundle.alignment_artifact.alignment_sha256,
        "review_decision_id": latest_review.id if latest_review else None,
        "review_revision": latest_review.revision if latest_review else None,
        "answer_key_source": answer_key.source if answer_key else None,
        "answer_key_sha256": answer_snapshot.sha256 if answer_snapshot else None,
        "rubric_sha256": rubric_digest,
        "rubric_criterion_count": len(rubric_snapshot),
        "rubric_total_points": rubric_total if rubric_snapshot else None,
        "rubric": [
            RubricInspectionItem(
                criterion_id=item.criterion_id,
                sort_order=item.sort_order,
                points=item.points,
                description_sha256=hashlib.sha256(item.description.encode("utf-8")).hexdigest(),
            )
            for item in rubric_snapshot
        ],
    }

    segments: list[SelectedAnswerSegment] = []
    evidence: list[GradeInputEvidenceItem] = []
    review_evidence_digest: str | None = None
    excluded_prompt_digest: str | None = None
    if review_read is not None and review_read.decision == "response_present":
        review_evidence_digest = _review_regions_digest(review_read.response_regions)
        excluded_prompt_digest = _review_regions_digest(review_read.excluded_prompt_regions)
        try:
            segments, evidence = _extract_confirmed_regions(
                bundle.canonical_document,
                review_read.response_regions,
            )
        except _InvalidReviewSource as error:
            reasons.append(error.reason_code)

    character_count = sum(len(item.text) for item in segments)
    text_byte_count = sum(len(item.text.encode("utf-8")) for item in segments)
    if text_byte_count > config.grade_input_max_text_bytes:
        reasons.append("selected_text_size_limit")
    reasons.extend(
        item.reason_code
        for item in evidence
        if item.representation == "unsupported" and item.reason_code is not None
    )

    status = _readiness_status(reasons, evidence)
    inspection = GradeInputInspection(
        status=status,
        reason_codes=sorted(set(reasons)),
        selected_region_count=len(review_read.response_regions) if review_read else 0,
        segment_count=len(segments),
        selected_text_characters=character_count,
        evidence_manifest=evidence,
        **inspection_base,
    )
    if status != "ready_text":
        return _Assessment(inspection, None)

    assert answer_snapshot is not None
    assert source_file is not None
    assert review_read is not None
    assert review_evidence_digest is not None
    assert excluded_prompt_digest is not None
    core = GradeInputCore(
        request_id=request_id,
        submission_id=submission_id,
        assignment_id=assignment.id,
        question=question_snapshot,
        canonical_artifact_id=bundle.canonical_artifact.id,
        canonical_sha256=bundle.canonical_artifact.canonical_sha256,
        canonical_schema_version=bundle.canonical_document.schema_version,
        canonical_source_sha256=bundle.canonical_artifact.source_sha256,
        alignment_artifact_id=bundle.alignment_artifact.id,
        alignment_sha256=bundle.alignment_artifact.alignment_sha256,
        aligner_version=bundle.alignment_artifact.aligner_version,
        assignment_questions_digest=bundle.questions_digest,
        review_decision_id=review_read.decision_id,
        review_revision=review_read.revision,
        review_request_sha256=latest_review.request_sha256,
        review_schema_version=review_read.review_schema_version,
        review_evidence_sha256=review_evidence_digest,
        excluded_prompt_regions_sha256=excluded_prompt_digest,
        source_file_sha256=source_file.sha256,
        answer_key=answer_snapshot,
        rubric_snapshot=rubric_snapshot,
        rubric_sha256=rubric_digest or "",
        selected_answer_segments=segments,
        evidence_manifest=evidence,
    )
    input_digest = json_sha256(core)
    inspection = inspection.model_copy(update={"input_sha256": input_digest})
    return _Assessment(inspection, core)


def _extract_confirmed_regions(
    document: CanonicalDocument,
    regions: list[ReviewEvidenceRegion],
) -> tuple[list[SelectedAnswerSegment], list[GradeInputEvidenceItem]]:
    node_index = _index_nodes(document)
    ordered = sorted(
        regions,
        key=lambda item: (
            item.page_index,
            item.reading_order,
            (
                node_index[item.content_pointer].traversal_order
                if item.content_pointer in node_index
                else -1
            ),
            -1 if item.text_start is None else item.text_start,
            -1 if item.text_end is None else item.text_end,
        ),
    )
    prior_ranges: dict[str, list[tuple[int, int]]] = {}
    segments: list[SelectedAnswerSegment] = []
    evidence: list[GradeInputEvidenceItem] = []

    for region in ordered:
        location = node_index.get(region.content_pointer)
        if location is None or not _region_matches_node(region, location):
            raise _InvalidReviewSource("review_region_not_in_canonical")
        node = location.node
        asset_refs = _safe_node_asset_refs(node, location.block)
        start, end = region.text_start, region.text_end
        if start is None:
            if end is not None or node.normalized_type != "image" or not asset_refs:
                raise _InvalidReviewSource("review_region_range_invalid")
            evidence.append(
                _evidence_item(region, "unsupported", "image_only_response", asset_refs)
            )
            continue
        if (
            end is None
            or node.content_kind != "scalar"
            or node.value is None
            or end > len(node.value)
        ):
            raise _InvalidReviewSource("review_region_range_invalid")
        for prior_start, prior_end in prior_ranges.setdefault(region.content_pointer, []):
            if max(start, prior_start) < min(end, prior_end):
                raise _InvalidReviewSource("review_region_ranges_overlap")
        prior_ranges[region.content_pointer].append((start, end))
        text = node.value[start:end]
        if not text.strip():
            raise _InvalidReviewSource("review_region_has_no_text")

        if node.normalized_type in {"text", "formula"}:
            reason = _text_representation_issue(text, asset_refs)
            if reason is not None:
                evidence.append(_evidence_item(region, "unsupported", reason, asset_refs))
                continue
            kind = "text" if node.normalized_type == "text" else "formula_text"
        else:
            reason = {
                "formula": "formula_is_not_textual",
                "image": "image_requires_visual_scoring",
                "table": "table_cells_not_normalized",
                "chart": "chart_requires_visual_scoring",
                "code": "unsupported_content_type",
                "list": "unsupported_content_type",
                "layout": "unsupported_content_type",
            }.get(node.normalized_type, "unsupported_content_type")
            evidence.append(_evidence_item(region, "unsupported", reason, asset_refs))
            continue

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        segments.append(
            SelectedAnswerSegment(
                segment_index=len(segments),
                kind=kind,
                content_pointer=region.content_pointer,
                page_index=region.page_index,
                page_number=region.page_number,
                source_block_id=region.source_block_id,
                reading_order=region.reading_order,
                text_start=start,
                text_end=end,
                text=text,
                text_sha256=digest,
            )
        )
        evidence.append(_evidence_item(region, kind, None, asset_refs))

    if not regions:
        raise _InvalidReviewSource("review_response_regions_missing")
    return segments, evidence


def _index_nodes(document: CanonicalDocument) -> dict[str, _NodeLocation]:
    indexed: dict[str, _NodeLocation] = {}
    traversal_order = 0

    def visit(
        node: CanonicalContentNode,
        pointer: str,
        page,
        block: CanonicalBlock,
    ) -> None:
        nonlocal traversal_order
        indexed[pointer] = _NodeLocation(page, block, node, traversal_order)
        traversal_order += 1
        for child_index, child in enumerate(node.children):
            visit(child, f"{pointer}/children/{child_index}", page, block)

    for page in document.pages:
        for block_index, block in enumerate(page.blocks):
            visit(
                block.content,
                f"/pages/{page.page_index}/blocks/{block_index}/content",
                page,
                block,
            )
    return indexed


def _region_matches_node(region: ReviewEvidenceRegion, location: _NodeLocation) -> bool:
    node, block, page = location.node, location.block, location.page
    if (
        region.page_index != page.page_index
        or region.page_number != page.page_number
        or region.source_block_id != block.block_id
        or region.reading_order != block.reading_order
        or region.source_type != node.source_type
        or region.normalized_type != node.normalized_type
        or region.bbox != (node.bbox or block.bbox)
    ):
        return False
    expected_asset_refs = [
        item.model_dump(mode="json") for item in _safe_node_asset_refs(node, block)
    ]
    actual_asset_refs = [item.model_dump(mode="json") for item in region.asset_refs]
    return actual_asset_refs == expected_asset_refs


def _safe_node_asset_refs(
    node: CanonicalContentNode,
    block: CanonicalBlock,
) -> list[GradeInputAssetReference]:
    references = list(node.asset_refs)
    if node.normalized_type == "image" and not references:
        references = list(block.asset_refs)
    safe: list[GradeInputAssetReference] = []
    seen: set[bytes] = set()
    for reference in references:
        item = ReviewAssetReference.model_validate(
            reference.model_dump(mode="json", exclude={"uri"})
        )
        serialized = canonical_json_bytes(item)
        if serialized in seen:
            continue
        seen.add(serialized)
        safe.append(GradeInputAssetReference.model_validate(item.model_dump(mode="json")))
    return safe


def _text_representation_issue(
    text: str,
    asset_refs: list[GradeInputAssetReference],
) -> str | None:
    """Return a stable reason when a selected scalar is not a complete text representation."""

    if asset_refs:
        return "canonical_asset_reference"
    if _REDACTED_INLINE_IMAGE in text:
        return "canonical_inline_image_placeholder"
    if _MARKDOWN_IMAGE_REFERENCE.search(text):
        return "markdown_image_reference"
    match = _EMBEDDED_NON_TEXT_TAG.search(text)
    if match is None:
        return None
    tag = match.group("tag").lower()
    if tag in _IMAGE_MARKUP_TAGS:
        return "embedded_image_markup"
    if tag in _TABLE_MARKUP_TAGS:
        return "embedded_table_markup"
    if tag in _EMBEDDED_MEDIA_TAGS:
        return "embedded_media_markup"
    return "structured_math_markup"


def _evidence_item(
    region: ReviewEvidenceRegion,
    representation: str,
    reason_code: str | None,
    asset_refs: list[GradeInputAssetReference],
) -> GradeInputEvidenceItem:
    return GradeInputEvidenceItem(
        content_pointer=region.content_pointer,
        page_index=region.page_index,
        page_number=region.page_number,
        source_block_id=region.source_block_id,
        reading_order=region.reading_order,
        normalized_type=region.normalized_type,
        text_start=region.text_start,
        text_end=region.text_end,
        representation=representation,
        reason_code=reason_code,
        asset_refs=asset_refs,
    )


class _InvalidReviewSource(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _review_regions_digest(regions: list[ReviewEvidenceRegion]) -> str:
    return json_sha256([region.model_dump(mode="json") for region in regions])


def _readiness_status(reasons: list[str], evidence: list[GradeInputEvidenceItem]) -> str:
    if any(
        reason.startswith("database_") or reason == "storage_unavailable" for reason in reasons
    ):
        return "source_unavailable"
    if any(
        "source" in reason or "canonical" in reason or "alignment" in reason
        for reason in reasons
    ):
        return "source_stale"
    if any(reason.startswith("answer_key") or reason.startswith("rubric") for reason in reasons):
        return "invalid_rubric"
    if any(
        reason.startswith("human_review") or reason.startswith("human_response")
        for reason in reasons
    ):
        return "review_required"
    if "assignment_not_published" in reasons:
        return "review_required"
    if any(item.representation == "unsupported" for item in evidence):
        return "unsupported_modality"
    if "selected_text_size_limit" in reasons:
        return "size_limit_exceeded"
    if reasons:
        return "source_stale"
    return "ready_text"


def _blocked(
    submission_id: UUID,
    question_id: UUID,
    status: str,
    reason: str,
    *,
    assignment_id: UUID | None = None,
) -> _Assessment:
    return _Assessment(
        GradeInputInspection(
            status=status,
            reason_codes=[reason],
            submission_id=submission_id,
            assignment_id=assignment_id,
            question_id=question_id,
        ),
        None,
    )


def _object_key(
    storage: S3ObjectStorage,
    submission_id: UUID,
    question_id: UUID,
    request_id: UUID,
) -> str:
    prefix = storage.object_key_prefix.strip("/")
    attempt_id = uuid4()
    key = f"grading/inputs/{submission_id}/{question_id}/{request_id}/{attempt_id}.json"
    return f"{prefix}/{key}" if prefix else key


async def _best_effort_delete(storage: S3ObjectStorage, object_key: str, reason: str) -> None:
    try:
        await storage.delete(object_key)
    except Exception as error:
        logger.warning(
            "GradeInput object cleanup failed (%s, %s)",
            reason,
            type(error).__name__,
        )


async def _read_object(
    storage: S3ObjectStorage,
    object_key: str,
    *,
    expected_size: int,
    expected_sha256: str,
    maximum: int,
) -> bytes:
    if expected_size <= 0 or expected_size > maximum:
        raise GradeInputArtifactCorruptError
    try:
        downloaded = await storage.download(object_key)
        payload = bytearray()
        async for chunk in downloaded.chunks():
            payload.extend(chunk)
            if len(payload) > maximum:
                raise GradeInputArtifactCorruptError
    except GradeInputArtifactCorruptError:
        raise
    except StorageUnavailableError:
        raise
    except Exception as error:
        raise StorageUnavailableError from error
    body = bytes(payload)
    if len(body) != expected_size or hashlib.sha256(body).hexdigest() != expected_sha256:
        raise GradeInputArtifactCorruptError
    return body
