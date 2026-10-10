"""Human review use cases over hash-verified Canonical and alignment evidence."""

import hashlib
import hmac
import json
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage
from huipi_cloud.modules.answer_alignment import service as alignment_service
from huipi_cloud.modules.answer_alignment.protocol import (
    AlignedAnswer,
    SourceRegion,
    UnassignedRegion,
)
from huipi_cloud.modules.answer_review import repository
from huipi_cloud.modules.answer_review.errors import (
    AnswerReviewQuestionNotFoundError,
    AnswerReviewRegionInvalidError,
    AnswerReviewSourceNotReadyError,
    AnswerReviewSubmissionNotFoundError,
)
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.answer_review.protocol import (
    ANSWER_REVIEW_SCHEMA_VERSION,
    AnswerReviewDecisionCreate,
    AnswerReviewDecisionRead,
    ReviewAssetReference,
    ReviewEvidenceRegion,
    ReviewRegionSelection,
)
from huipi_cloud.modules.canonical_documents.protocol import (
    CanonicalAssetReference,
    CanonicalBlock,
    CanonicalContentNode,
    CanonicalDocument,
)


@dataclass(frozen=True)
class _NodeLocation:
    pointer: str
    parent_pointer: str | None
    page_index: int
    page_number: int
    block: CanonicalBlock
    node: CanonicalContentNode


async def record_decision(
    session_factory: repository.SessionFactory,
    storage: S3ObjectStorage,
    *,
    submission_id: UUID,
    question_id: UUID,
    request_id: UUID,
    payload: AnswerReviewDecisionCreate,
) -> tuple[AnswerReviewDecisionRead, bool]:
    """Validate human-selected evidence, then append one version-bound revision."""

    bundle = await alignment_service.load_verified_alignment_bundle(
        session_factory,
        storage,
        submission_id,
    )
    question = next(
        (item for item in bundle.context.questions if item.id == question_id),
        None,
    )
    if question is None:
        raise AnswerReviewQuestionNotFoundError
    aligned_answer = next(
        (item for item in bundle.alignment_document.answers if item.question_id == question_id),
        None,
    )
    if aligned_answer is None:
        raise AnswerReviewSourceNotReadyError

    node_index = _index_canonical_nodes(bundle.canonical_document)
    response_regions = _materialize_regions(payload.response_regions, node_index)
    prompt_regions = _materialize_regions(payload.excluded_prompt_regions, node_index)
    uncertain_regions = _materialize_regions(
        payload.uncertain_regions,
        node_index,
        allow_block_asset_context=True,
    )
    _validate_selection_conflicts(payload, node_index)
    _validate_question_ownership(
        payload,
        question_id,
        bundle.alignment_document.answers,
        bundle.alignment_document.unassigned_regions,
        node_index,
    )
    request_sha256 = _request_digest(
        submission_id=submission_id,
        question_id=question_id,
        canonical_document_id=bundle.canonical_artifact.id,
        canonical_sha256=bundle.canonical_artifact.canonical_sha256,
        alignment_artifact_id=bundle.alignment_artifact.id,
        alignment_sha256=bundle.alignment_artifact.alignment_sha256,
        aligner_version=bundle.alignment_artifact.aligner_version,
        assignment_questions_digest=bundle.questions_digest,
        review_schema_version=ANSWER_REVIEW_SCHEMA_VERSION,
        payload=payload,
    )

    record, created = await repository.append_decision(
        session_factory,
        submission_id=submission_id,
        assignment_id=bundle.context.assignment_id,
        question_id=question_id,
        alignment_artifact_id=bundle.alignment_artifact.id,
        alignment_sha256=bundle.alignment_artifact.alignment_sha256,
        alignment_status=aligned_answer.matching_status,
        aligner_version=bundle.alignment_artifact.aligner_version,
        assignment_questions_digest_value=bundle.questions_digest,
        canonical_document_id=bundle.canonical_artifact.id,
        canonical_sha256=bundle.canonical_artifact.canonical_sha256,
        review_schema_version=ANSWER_REVIEW_SCHEMA_VERSION,
        decision=payload.decision,
        response_regions=[item.model_dump(mode="json") for item in response_regions],
        excluded_prompt_regions=[item.model_dump(mode="json") for item in prompt_regions],
        uncertain_regions=[item.model_dump(mode="json") for item in uncertain_regions],
        reason_codes=list(payload.reason_codes),
        reviewer_ref=payload.reviewer_ref,
        request_id=request_id,
        request_sha256=request_sha256,
    )
    return decision_read(record), created


async def list_decisions(
    session: AsyncSession,
    submission_id: UUID,
    *,
    question_id: UUID | None = None,
) -> list[AnswerReviewDecisionRead]:
    if await repository.get_submission(session, submission_id) is None:
        raise AnswerReviewSubmissionNotFoundError
    records = await repository.list_decisions(
        session,
        submission_id,
        question_id=question_id,
    )
    return [decision_read(record) for record in records]


def is_current_for_bundle(
    record: AnswerReviewDecisionRead,
    bundle: alignment_service.VerifiedAlignmentBundle,
) -> bool:
    """Only exact matching source versions make a revision eligible for use."""

    return (
        record.submission_id == bundle.context.submission_id
        and record.assignment_id == bundle.context.assignment_id
        and record.alignment_artifact_id == bundle.alignment_artifact.id
        and record.alignment_sha256 == bundle.alignment_artifact.alignment_sha256
        and record.aligner_version == bundle.alignment_artifact.aligner_version
        and record.assignment_questions_digest == bundle.questions_digest
        and record.canonical_document_id == bundle.canonical_artifact.id
        and record.canonical_sha256 == bundle.canonical_artifact.canonical_sha256
    )


def decision_read(record: AnswerReviewDecision) -> AnswerReviewDecisionRead:
    """Validate JSONB before it leaves the trusted persistence boundary."""

    return AnswerReviewDecisionRead(
        decision_id=record.id,
        submission_id=record.submission_id,
        assignment_id=record.assignment_id,
        question_id=record.question_id,
        question_number=record.question_number,
        alignment_status=record.alignment_status,
        alignment_artifact_id=record.alignment_artifact_id,
        alignment_sha256=record.alignment_sha256,
        aligner_version=record.aligner_version,
        assignment_questions_digest=record.assignment_questions_digest,
        canonical_document_id=record.canonical_document_id,
        canonical_sha256=record.canonical_sha256,
        review_schema_version=record.review_schema_version,
        decision=record.decision,
        response_regions=[
            ReviewEvidenceRegion.model_validate(item) for item in record.response_regions
        ],
        excluded_prompt_regions=[
            ReviewEvidenceRegion.model_validate(item) for item in record.excluded_prompt_regions
        ],
        uncertain_regions=[
            ReviewEvidenceRegion.model_validate(item) for item in record.uncertain_regions
        ],
        reason_codes=record.reason_codes,
        reviewer_ref=record.reviewer_ref,
        request_id=record.request_id,
        revision=record.revision,
        supersedes_decision_id=record.supersedes_decision_id,
        created_at=record.created_at,
    )


def _index_canonical_nodes(
    document: CanonicalDocument,
) -> dict[str, _NodeLocation]:
    locations: dict[str, _NodeLocation] = {}

    def visit(
        node: CanonicalContentNode,
        pointer: str,
        parent_pointer: str | None,
        page_index: int,
        page_number: int,
        block: CanonicalBlock,
    ) -> None:
        locations[pointer] = _NodeLocation(
            pointer,
            parent_pointer,
            page_index,
            page_number,
            block,
            node,
        )
        for child_index, child in enumerate(node.children):
            visit(
                child,
                f"{pointer}/children/{child_index}",
                pointer,
                page_index,
                page_number,
                block,
            )

    for page in document.pages:
        for block_index, block in enumerate(page.blocks):
            visit(
                block.content,
                f"/pages/{page.page_index}/blocks/{block_index}/content",
                None,
                page.page_index,
                page.page_number,
                block,
            )
    return locations


def _validate_question_ownership(
    payload: AnswerReviewDecisionCreate,
    question_id: UUID,
    answers: list[AlignedAnswer],
    unassigned_regions: list[UnassignedRegion],
    node_index: dict[str, _NodeLocation],
) -> None:
    """Reject evidence already assigned to another Question by P2-D1.

    A reviewer can still correct a missing or uncertain automatic association.
    That exception must be explicit and stay uncertain; this record does not
    silently rewrite the immutable alignment result.
    """

    other_question_regions = [
        region
        for answer in answers
        if answer.question_id != question_id
        for region in answer.source_regions
    ]
    unassigned_source_regions = [
        region for item in unassigned_regions for region in item.source_regions
    ]
    selections = [
        region
        for regions in (
            payload.response_regions,
            payload.excluded_prompt_regions,
            payload.uncertain_regions,
        )
        for region in regions
    ]
    unresolved = [
        selection
        for selection in selections
        if any(
            _selection_overlaps_source(selection, source_region, node_index)
            for source_region in (*other_question_regions, *unassigned_source_regions)
        )
    ]
    if not unresolved:
        return
    uncertain_keys = {_selection_key(selection) for selection in payload.uncertain_regions}
    if (
        payload.decision != "uncertain"
        or not {
            "response_not_linked_to_question",
            "shared_figure_attribution_unclear",
        }.intersection(payload.reason_codes)
        or not payload.uncertain_regions
        or not {_selection_key(selection) for selection in unresolved}.issubset(uncertain_keys)
    ):
        raise AnswerReviewRegionInvalidError


def _selection_overlaps_source(
    selection: ReviewRegionSelection,
    source_region: SourceRegion,
    node_index: dict[str, _NodeLocation],
) -> bool:
    if selection.content_pointer == source_region.content_pointer:
        if selection.text_start is None or source_region.text_start is None:
            return True
        assert selection.text_end is not None and source_region.text_end is not None
        return max(selection.text_start, source_region.text_start) < min(
            selection.text_end,
            source_region.text_end,
        )

    selected_location = node_index.get(selection.content_pointer)
    source_location = node_index.get(source_region.content_pointer)
    if selected_location is None or source_location is None:
        return False

    if _nodes_overlap(
        selection.content_pointer,
        selection.text_start,
        selection.text_end,
        source_region.content_pointer,
        source_region.text_start,
        source_region.text_end,
        node_index,
    ):
        return True

    if _selection_asset_keys(selection, selected_location) & _source_asset_keys(
        source_region,
        source_location,
    ):
        return True

    # A Block-level image link has no character or child-node position. If the
    # reviewer selects that image and another Question has a source in the same
    # Block, the image cannot be claimed as exclusive evidence for this one.
    selected_node_asset_keys = {
        _asset_reference_key(reference) for reference in selected_location.node.asset_refs
    }
    block_asset_keys = {
        _asset_reference_key(reference) for reference in selected_location.block.asset_refs
    }
    return (
        selection.text_start is None
        and selected_location.node.normalized_type == "image"
        and selected_location.block.block_id == source_region.source_block_id
        and bool(block_asset_keys)
        and (not selected_node_asset_keys or bool(block_asset_keys & selected_node_asset_keys))
    )


def _nodes_overlap(
    first_pointer: str,
    first_start: int | None,
    first_end: int | None,
    second_pointer: str,
    second_start: int | None,
    second_end: int | None,
    node_index: dict[str, _NodeLocation],
) -> bool:
    if first_pointer == second_pointer:
        if first_start is None or second_start is None:
            return True
        assert first_end is not None and second_end is not None
        return max(first_start, second_start) < min(first_end, second_end)
    return _is_ancestor(first_pointer, second_pointer, node_index) or _is_ancestor(
        second_pointer,
        first_pointer,
        node_index,
    )


def _is_ancestor(
    ancestor_pointer: str,
    child_pointer: str,
    node_index: dict[str, _NodeLocation],
) -> bool:
    child = node_index.get(child_pointer)
    while child is not None and child.parent_pointer is not None:
        if child.parent_pointer == ancestor_pointer:
            return True
        child = node_index.get(child.parent_pointer)
    return False


def _validate_selection_conflicts(
    payload: AnswerReviewDecisionCreate,
    node_index: dict[str, _NodeLocation],
) -> None:
    """Reject overlapping content or duplicate visual assets across roles."""

    selections = [
        selection
        for regions in (
            payload.response_regions,
            payload.excluded_prompt_regions,
            payload.uncertain_regions,
        )
        for selection in regions
    ]
    for index, first in enumerate(selections):
        for second in selections[index + 1 :]:
            first_location = node_index[first.content_pointer]
            second_location = node_index[second.content_pointer]
            if _nodes_overlap(
                first.content_pointer,
                first.text_start,
                first.text_end,
                second.content_pointer,
                second.text_start,
                second.text_end,
                node_index,
            ) or _selection_asset_keys(first, first_location) & _selection_asset_keys(
                second,
                second_location,
            ):
                raise AnswerReviewRegionInvalidError


def _selection_key(selection: ReviewRegionSelection) -> tuple[str, int | None, int | None]:
    return (selection.content_pointer, selection.text_start, selection.text_end)


def _materialize_regions(
    selections: list[ReviewRegionSelection],
    node_index: dict[str, _NodeLocation],
    *,
    allow_block_asset_context: bool = False,
) -> list[ReviewEvidenceRegion]:
    evidence: list[ReviewEvidenceRegion] = []
    for selection in selections:
        location = node_index.get(selection.content_pointer)
        if location is None:
            raise AnswerReviewRegionInvalidError
        node = location.node
        if selection.text_start is not None:
            if node.content_kind != "scalar" or node.value is None:
                raise AnswerReviewRegionInvalidError
            if selection.text_end is None or selection.text_end > len(node.value):
                raise AnswerReviewRegionInvalidError
        elif node.value not in {None, ""}:
            # Text must be selected with explicit Unicode code-point offsets.
            raise AnswerReviewRegionInvalidError
        elif node.normalized_type == "image":
            if not (node.asset_refs or location.block.asset_refs):
                raise AnswerReviewRegionInvalidError
        elif not (
            allow_block_asset_context
            and location.parent_pointer is None
            and location.block.asset_refs
        ):
            # Only an explicit image node can be a confirmed whole-node
            # selection. A root container may cite a Block-level image only
            # in the uncertain role; that link must not qualify its children.
            raise AnswerReviewRegionInvalidError

        references = list(node.asset_refs)
        if node.normalized_type == "image" and not references:
            references = list(location.block.asset_refs)
        elif (
            selection.text_start is None
            and allow_block_asset_context
            and location.parent_pointer is None
            and not references
        ):
            references = list(location.block.asset_refs)
        safe_references = _safe_asset_references(references)
        if node.normalized_type == "image" and not safe_references:
            raise AnswerReviewRegionInvalidError
        evidence.append(
            ReviewEvidenceRegion(
                content_pointer=selection.content_pointer,
                page_index=location.page_index,
                page_number=location.page_number,
                source_block_id=location.block.block_id,
                reading_order=location.block.reading_order,
                bbox=node.bbox or location.block.bbox,
                source_type=node.source_type,
                normalized_type=node.normalized_type,
                text_start=selection.text_start,
                text_end=selection.text_end,
                asset_refs=safe_references,
            )
        )
    return evidence


def _safe_asset_references(
    references: Iterable[CanonicalAssetReference],
) -> list[ReviewAssetReference]:
    seen: set[tuple[str, ...]] = set()
    safe: list[ReviewAssetReference] = []
    for reference in references:
        item = ReviewAssetReference.model_validate(
            reference.model_dump(mode="json", exclude={"uri"})
        )
        key = _asset_reference_key(reference)
        if key in seen:
            continue
        seen.add(key)
        safe.append(item)
    return safe


def _selection_asset_keys(
    selection: ReviewRegionSelection,
    location: _NodeLocation,
) -> set[tuple[str, ...]]:
    if selection.text_start is not None or location.node.normalized_type != "image":
        return set()
    references = list(location.node.asset_refs)
    if not references:
        references = list(location.block.asset_refs)
    return {_asset_reference_key(reference) for reference in references}


def _source_asset_keys(
    source_region: SourceRegion,
    location: _NodeLocation,
) -> set[tuple[str, ...]]:
    return {
        _asset_reference_key(reference)
        for reference in (*source_region.asset_refs, *location.node.asset_refs)
    }


def _asset_reference_key(reference: CanonicalAssetReference) -> tuple[str, ...]:
    if reference.kind == "stored":
        return ("stored", str(reference.asset_id))
    if reference.kind == "external":
        return ("external", reference.uri or "")
    return (
        "inline_redacted",
        reference.sha256 or "",
        str(reference.size_bytes),
        reference.content_type or "",
    )


def _request_digest(
    *,
    submission_id: UUID,
    question_id: UUID,
    canonical_document_id: UUID,
    canonical_sha256: str,
    alignment_artifact_id: UUID,
    alignment_sha256: str,
    aligner_version: str,
    assignment_questions_digest: str,
    review_schema_version: str,
    payload: AnswerReviewDecisionCreate,
) -> str:
    value = {
        "submission_id": str(submission_id),
        "question_id": str(question_id),
        "canonical_document_id": str(canonical_document_id),
        "canonical_sha256": canonical_sha256,
        "alignment_artifact_id": str(alignment_artifact_id),
        "alignment_sha256": alignment_sha256,
        "aligner_version": aligner_version,
        "assignment_questions_digest": assignment_questions_digest,
        "review_schema_version": review_schema_version,
        "payload": payload.model_dump(mode="json"),
    }
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def export_records(records: list[AnswerReviewDecisionRead]) -> dict[str, object]:
    """Create a redacted export without source text, reviewer IDs, keys, or raw UUIDs."""

    salt = secrets.token_bytes(32)

    def pseudonym(value: UUID | str) -> str:
        return hmac.new(salt, str(value).encode("utf-8"), hashlib.sha256).hexdigest()

    exported = []
    for item in records:
        exported.append(
            {
                "decision_id": pseudonym(item.decision_id),
                "submission_id": pseudonym(item.submission_id),
                "assignment_id": pseudonym(item.assignment_id),
                "question_id": pseudonym(item.question_id),
                "question_number": item.question_number,
                "alignment_status": item.alignment_status,
                "alignment_artifact_ref": pseudonym(item.alignment_artifact_id),
                "alignment_digest_ref": pseudonym(item.alignment_sha256),
                "aligner_version": item.aligner_version,
                "question_set_digest_ref": pseudonym(item.assignment_questions_digest),
                "canonical_document_ref": pseudonym(item.canonical_document_id),
                "canonical_digest_ref": pseudonym(item.canonical_sha256),
                "review_schema_version": item.review_schema_version,
                "decision": item.decision,
                "response_regions": _export_regions(item.response_regions, pseudonym),
                "excluded_prompt_regions": _export_regions(
                    item.excluded_prompt_regions,
                    pseudonym,
                ),
                "uncertain_regions": _export_regions(item.uncertain_regions, pseudonym),
                "reason_codes": item.reason_codes,
                "revision": item.revision,
                "supersedes_decision_id": (
                    None
                    if item.supersedes_decision_id is None
                    else pseudonym(item.supersedes_decision_id)
                ),
            }
        )
    return {
        "schema_name": "huipi.answer.review.export",
        "schema_version": ANSWER_REVIEW_SCHEMA_VERSION,
        "records": exported,
    }


def _export_regions(regions, pseudonym):
    values = []
    for region in regions:
        values.append(
            {
                "content_pointer": region.content_pointer,
                "page_index": region.page_index,
                "page_number": region.page_number,
                "source_block_ref": pseudonym(region.source_block_id),
                "reading_order": region.reading_order,
                "bbox": region.bbox,
                "source_type": region.source_type,
                "normalized_type": region.normalized_type,
                "text_start": region.text_start,
                "text_end": region.text_end,
                "asset_refs": [
                    {
                        **reference.model_dump(mode="json"),
                        "asset_id": (
                            None if reference.asset_id is None else pseudonym(reference.asset_id)
                        ),
                    }
                    for reference in region.asset_refs
                ],
            }
        )
    return values
