"""Review-first VLM proposal flow with immutable S3 and PostgreSQL records."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from huipi_cloud.core.config import Settings, settings
from huipi_cloud.infrastructure.storage.s3 import (
    S3ObjectStorage,
    StorageObjectNotFoundError,
    StorageUnavailableError,
)
from huipi_cloud.infrastructure.visual_evidence.provider import (
    AlignmentRegionSummary,
    VisionProvider,
    VisionProviderResponse,
)
from huipi_cloud.infrastructure.visual_evidence.render import (
    RenderedPage,
    render_selected_pages,
    transform_bbox_to_source,
)
from huipi_cloud.modules.answer_alignment.protocol import AlignedAnswer, SourceRegion
from huipi_cloud.modules.answer_alignment.service import load_verified_alignment_bundle
from huipi_cloud.modules.answer_alignment.structure import canonical_atoms
from huipi_cloud.modules.visual_evidence import repository
from huipi_cloud.modules.visual_evidence.errors import (
    VisualEvidenceConfigurationError,
    VisualEvidenceConflictError,
    VisualEvidenceInputError,
    VisualEvidenceLimitError,
    VisualEvidenceNotFoundError,
    VisualEvidenceNotReadyError,
    VisualEvidenceProtocolError,
    VisualEvidenceSourceChangedError,
    VisualEvidenceStorageError,
)
from huipi_cloud.modules.visual_evidence.models import VisualEvidenceArtifact
from huipi_cloud.modules.visual_evidence.protocol import (
    VISUAL_PREPROCESSING_VERSION,
    VISUAL_PROMPT_VERSION,
    VISUAL_REASON_CODES,
    RenderedPageMetadata,
    VisualEvidenceModelReply,
    VisualEvidenceProposal,
    VisualEvidenceRegion,
)

logger = logging.getLogger(__name__)
_READ_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class VisualEvidenceDryRun:
    """Safe resource and source summary returned without calling a model."""

    submission_id: UUID
    question_id: UUID
    selected_page_indexes: tuple[int, ...]
    original_file_sha256: str
    model_input_digest: str
    rendered_pages: tuple[RenderedPageMetadata, ...]
    total_rendered_bytes: int


async def analyze_question_visual_evidence(
    session_factory: repository.SessionFactory,
    storage: S3ObjectStorage,
    provider: VisionProvider | None,
    *,
    submission_id: UUID,
    question_id: UUID,
    request_id: UUID,
    dry_run: bool = False,
    config: Settings = settings,
) -> VisualEvidenceArtifact | VisualEvidenceDryRun:
    """Analyze selected original pages; persist a model proposal, never a review decision."""
    if not dry_run and (not config.visual_evidence_enabled or provider is None):
        raise VisualEvidenceConfigurationError("visual_model_disabled_or_missing")
    bundle = await load_verified_alignment_bundle(
        session_factory, storage, submission_id, config=config
    )
    answer = next(
        (item for item in bundle.alignment_document.answers if item.question_id == question_id),
        None,
    )
    question = next((item for item in bundle.context.questions if item.id == question_id), None)
    if answer is None or question is None:
        raise VisualEvidenceNotFoundError("question_not_in_submission_assignment")
    page_indexes = _question_pages(answer)
    if not page_indexes:
        raise VisualEvidenceNotReadyError("question_has_no_verified_source_pages")
    if len(page_indexes) > config.visual_evidence_max_pages_per_question:
        raise VisualEvidenceLimitError("too_many_question_pages")
    if any(page_index >= bundle.canonical_document.page_count for page_index in page_indexes):
        raise VisualEvidenceSourceChangedError("alignment_page_outside_canonical")

    async with session_factory() as session:
        source_context = await repository.load_context(session, submission_id)
    if (
        source_context.canonical.id != bundle.canonical_artifact.id
        or source_context.alignment_artifact.id != bundle.alignment_artifact.id
        or source_context.alignment.assignment_id != bundle.context.assignment_id
        or _question_snapshot(source_context.alignment.questions)
        != _question_snapshot(bundle.context.questions)
    ):
        raise VisualEvidenceSourceChangedError
    if source_context.canonical.source_sha256 != source_context.source_file.sha256:
        raise VisualEvidenceSourceChangedError("original_file_canonical_sha_mismatch")
    if (
        source_context.source_file.bucket != storage.bucket
        or bundle.canonical_artifact.bucket != storage.bucket
        or bundle.alignment_artifact.bucket != storage.bucket
    ):
        raise VisualEvidenceStorageError("source_bucket_mismatch")
    if source_context.source_file.size_bytes > min(
        config.max_upload_size_bytes, config.visual_evidence_max_original_bytes
    ):
        raise VisualEvidenceLimitError("original_file_size_limit_exceeded")
    if source_context.source_file.size_bytes < 1:
        raise VisualEvidenceInputError("original_file_size_invalid")

    with tempfile.TemporaryDirectory(prefix="huipi-visual-evidence-") as temp_dir:
        original_path = Path(temp_dir) / "source.bin"
        source_digest, source_size = await _download_original(
            storage,
            source_context.source_file,
            original_path,
            maximum=min(config.max_upload_size_bytes, config.visual_evidence_max_original_bytes),
        )
        if source_digest != source_context.source_file.sha256:
            raise VisualEvidenceInputError("original_file_sha256_mismatch")
        if source_size != source_context.source_file.size_bytes:
            raise VisualEvidenceInputError("original_file_size_mismatch")
        _validate_original_kind(
            source_context.source_file.original_filename,
            source_context.source_file.content_type,
            original_path,
        )
        # The rendered thread receives a bounded immutable byte buffer. If the awaiter is
        # cancelled, its worker thread cannot race TemporaryDirectory cleanup on this path.
        render_source = await asyncio.to_thread(original_path.read_bytes)
        pages = await asyncio.to_thread(
            render_selected_pages,
            render_source,
            content_type=source_context.source_file.content_type,
            selected_page_indexes=page_indexes,
            config=config,
        )
        total_rendered_bytes = sum(len(page.image_bytes) for page in pages)
        if total_rendered_bytes > config.visual_evidence_max_total_input_bytes:
            raise VisualEvidenceLimitError("rendered_input_total_limit_exceeded")

        model_id = getattr(provider, "model_id", config.visual_evidence_model)
        provider_fingerprint = getattr(
            provider,
            "configuration_fingerprint",
            hashlib.sha256(type(provider).__qualname__.encode("utf-8")).hexdigest()
            if provider is not None
            else "disabled",
        )
        model_input_digest = _input_digest(
            submission_id=submission_id,
            question_id=question_id,
            question_number=question.question_number,
            question_stem=question.stem,
            original_sha256=source_digest,
            canonical_id=bundle.canonical_artifact.id,
            canonical_sha256=bundle.canonical_artifact.canonical_sha256 or "",
            alignment_id=bundle.alignment_artifact.id,
            alignment_sha256=bundle.alignment_artifact.alignment_sha256,
            questions_digest=bundle.questions_digest,
            page_metadata=[_page_metadata(page).model_dump(mode="json") for page in pages],
            model_id=model_id,
            provider_fingerprint=provider_fingerprint,
            config=config,
        )
        if dry_run:
            return VisualEvidenceDryRun(
                submission_id=submission_id,
                question_id=question_id,
                selected_page_indexes=tuple(page_indexes),
                original_file_sha256=source_digest,
                model_input_digest=model_input_digest,
                rendered_pages=tuple(_page_metadata(page) for page in pages),
                total_rendered_bytes=total_rendered_bytes,
            )
        async with session_factory() as session:
            existing = await repository.get_request(
                session,
                submission_id=submission_id,
                question_id=question_id,
                request_id=request_id,
            )
        if existing is not None:
            if existing.model_input_digest != model_input_digest:
                raise VisualEvidenceConflictError
            return existing

        summaries = [
            AlignmentRegionSummary(
                page_index=region.page_index,
                bbox=region.bbox,
                normalized_type=region.normalized_type,
            )
            for region in answer.source_regions
        ]
        # Every DB read transaction is closed before calling the provider.
        provider_result = await provider.analyze(
            question_number=question.question_number,
            question_stem=question.stem,
            source_regions=summaries,
            pages=pages,
        )
        if isinstance(provider_result, VisionProviderResponse):
            reply = provider_result.reply
            model_elapsed_ms = provider_result.elapsed_ms
            provider_usage = provider_result.usage
        elif isinstance(provider_result, VisualEvidenceModelReply):
            reply = provider_result
            model_elapsed_ms = None
            provider_usage = None
        else:
            raise VisualEvidenceProtocolError("provider_reply_not_validated")
        if any(
            item.page_index not in page_indexes for item in reply.visual_regions
        ):
            raise VisualEvidenceProtocolError("model_referred_to_unsubmitted_page")

        proposal_id = uuid4()
        proposal = VisualEvidenceProposal(
            proposal_id=proposal_id,
            request_id=request_id,
            submission_id=submission_id,
            assignment_id=bundle.context.assignment_id,
            question_id=question_id,
            question_number=question.question_number,
            canonical_artifact_id=bundle.canonical_artifact.id,
            canonical_sha256=bundle.canonical_artifact.canonical_sha256 or "",
            alignment_artifact_id=bundle.alignment_artifact.id,
            alignment_sha256=bundle.alignment_artifact.alignment_sha256,
            original_file_sha256=source_digest,
            model_id=model_id,
            prompt_version=VISUAL_PROMPT_VERSION,
            preprocessing_version=VISUAL_PREPROCESSING_VERSION,
            model_input_digest=model_input_digest,
            selected_page_indexes=page_indexes,
            rendered_pages=[_page_metadata(page) for page in pages],
            outcome=reply.outcome,
            visual_regions=_attribute_regions(
                reply,
                answer,
                bundle.alignment_document.answers,
                bundle.canonical_document,
                {page.page_index: page for page in pages},
            ),
            reason_codes=list(reply.reason_codes),
            model_elapsed_ms=model_elapsed_ms,
            provider_usage=provider_usage,
            created_at=datetime.now(UTC),
        )
        payload = json.dumps(
            proposal.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > config.visual_evidence_max_proposal_bytes:
            raise VisualEvidenceLimitError("proposal_size_limit_exceeded")

        object_key = _proposal_object_key(
            storage.object_key_prefix, submission_id, question_id, proposal_id
        )
        try:
            await storage.upload_fileobj(BytesIO(payload), object_key, "application/json")
            metadata = await storage.head_object(object_key)
            if metadata.get("content_length") != len(payload):
                raise StorageUnavailableError("visual proposal object size mismatch")
        except StorageUnavailableError as error:
            await _delete_unindexed_object(storage, session_factory, object_key)
            raise VisualEvidenceStorageError("proposal_upload_failed") from error

        values: dict[str, object] = {
            "id": proposal_id,
            "request_id": request_id,
            "submission_id": submission_id,
            "assignment_id": bundle.context.assignment_id,
            "question_id": question_id,
            "question_number": question.question_number,
            "canonical_artifact_id": bundle.canonical_artifact.id,
            "canonical_sha256": bundle.canonical_artifact.canonical_sha256,
            "alignment_artifact_id": bundle.alignment_artifact.id,
            "alignment_sha256": bundle.alignment_artifact.alignment_sha256,
            "original_file_sha256": source_digest,
            "model_id": model_id,
            "prompt_version": VISUAL_PROMPT_VERSION,
            "preprocessing_version": VISUAL_PREPROCESSING_VERSION,
            "model_input_digest": model_input_digest,
            "outcome": reply.outcome,
            "proposal_bucket": storage.bucket,
            "proposal_object_key": object_key,
            "proposal_sha256": hashlib.sha256(payload).hexdigest(),
            "proposal_size_bytes": len(payload),
        }
        try:
            record = await repository.register_success(
                session_factory,
                values=values,
                expected_question_stem=question.stem,
                expected_source_file_id=source_context.source_file.id,
                expected_source_object_key=source_context.source_file.object_key,
                expected_source_bucket=source_context.source_file.bucket,
                expected_source_size=source_context.source_file.size_bytes,
            )
        except Exception as error:
            existing = await _lookup_request_safely(
                session_factory,
                submission_id=submission_id,
                question_id=question_id,
                request_id=request_id,
            )
            if existing is not None:
                await _delete_unindexed_object(storage, session_factory, object_key)
                if existing.model_input_digest == model_input_digest:
                    return existing
                raise VisualEvidenceConflictError from error
            await _delete_unindexed_object(storage, session_factory, object_key)
            if isinstance(error, (VisualEvidenceConflictError, VisualEvidenceSourceChangedError)):
                raise
            logger.error("Visual proposal registration failed (%s)", type(error).__name__)
            raise VisualEvidenceStorageError("proposal_index_registration_failed") from error
        if record.proposal_object_key != object_key:
            await _delete_unindexed_object(storage, session_factory, object_key)
        return record


async def _download_original(
    storage: S3ObjectStorage,
    source_file,
    target_path: Path,
    *,
    maximum: int,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        downloaded = await storage.download(source_file.object_key)
        with target_path.open("wb") as target:
            async for chunk in downloaded.chunks(_READ_CHUNK_SIZE):
                size += len(chunk)
                if size > maximum or size > source_file.size_bytes:
                    raise VisualEvidenceLimitError("original_file_size_limit_exceeded")
                target.write(chunk)
                digest.update(chunk)
    except StorageObjectNotFoundError as error:
        raise VisualEvidenceInputError("original_file_missing") from error
    except StorageUnavailableError as error:
        raise VisualEvidenceStorageError("original_file_download_failed") from error
    return digest.hexdigest(), size


def _validate_original_kind(filename: str, content_type: str, path: Path) -> None:
    extension = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    expected = {
        "application/pdf": (".pdf",),
        "image/jpeg": (".jpg", ".jpeg"),
        "image/png": (".png",),
    }
    if content_type not in expected or extension not in expected[content_type]:
        raise VisualEvidenceInputError("original_file_metadata_invalid")
    with path.open("rb") as source:
        signature = source.read(8)
    valid = {
        "application/pdf": signature.startswith(b"%PDF-"),
        "image/jpeg": signature.startswith(b"\xff\xd8\xff"),
        "image/png": signature.startswith(b"\x89PNG\r\n\x1a\n"),
    }
    if not valid[content_type]:
        raise VisualEvidenceInputError("original_file_signature_invalid")


def _question_pages(answer: AlignedAnswer) -> list[int]:
    pages = {region.page_index for region in answer.source_regions}
    pages.update(item.page_index for item in answer.evidence)
    return sorted(pages)


def _question_snapshot(questions: list) -> tuple[tuple[str, int, str, str], ...]:
    return tuple(
        sorted(
            (str(item.id), item.question_number, item.question_type, item.stem)
            for item in questions
        )
    )


def _page_metadata(page: RenderedPage) -> RenderedPageMetadata:
    return RenderedPageMetadata(
        page_index=page.page_index,
        source_width=float(page.source_width),
        source_height=float(page.source_height),
        source_unit=page.source_unit,
        source_rotation_degrees=page.source_rotation_degrees,
        rendered_width=page.rendered_width,
        rendered_height=page.rendered_height,
        effective_dpi=page.effective_dpi,
        exif_orientation=page.exif_orientation,
        mapping_eligible=page.mapping_eligible,
        mapping_reason=page.mapping_reason,
        transform=page.transform,
        rendered_image_sha256=page.sha256,
    )


def _input_digest(
    *,
    submission_id: UUID,
    question_id: UUID,
    question_number: int,
    question_stem: str,
    original_sha256: str,
    canonical_id: UUID,
    canonical_sha256: str,
    alignment_id: UUID,
    alignment_sha256: str,
    questions_digest: str,
    page_metadata: list[dict[str, object]],
    model_id: str,
    provider_fingerprint: str,
    config: Settings,
) -> str:
    values = {
        "submission_id": str(submission_id),
        "question_id": str(question_id),
        "question_number": question_number,
        "question_stem_sha256": hashlib.sha256(question_stem.encode("utf-8")).hexdigest(),
        "original_file_sha256": original_sha256,
        "canonical_artifact_id": str(canonical_id),
        "canonical_sha256": canonical_sha256,
        "alignment_artifact_id": str(alignment_id),
        "alignment_sha256": alignment_sha256,
        "questions_digest": questions_digest,
        "rendered_pages": page_metadata,
        "model_id": model_id,
        "provider_fingerprint": provider_fingerprint,
        "prompt_version": VISUAL_PROMPT_VERSION,
        "preprocessing_version": VISUAL_PREPROCESSING_VERSION,
        "render_dpi": config.visual_evidence_render_dpi,
        "max_render_edge": config.visual_evidence_max_render_edge,
        "max_image_pixels": config.visual_evidence_max_image_pixels,
        "max_output_tokens": config.visual_evidence_max_output_tokens,
    }
    payload = json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _attribute_regions(
    reply: VisualEvidenceModelReply,
    target: AlignedAnswer,
    all_answers: list[AlignedAnswer],
    canonical,
    pages_by_index: dict[int, RenderedPage],
) -> list[VisualEvidenceRegion]:
    atoms = {atom.content_pointer: atom for atom in canonical_atoms(canonical)}
    target_ids = {region.content_pointer for region in target.source_regions}
    result: list[VisualEvidenceRegion] = []
    for raw_region in reply.visual_regions:
        page = pages_by_index[raw_region.page_index]
        reasons = list(raw_region.reason_codes)
        if not page.mapping_eligible or target.matching_status != "aligned":
            if not page.mapping_eligible:
                reasons.append("source_mapping_unavailable")
            else:
                reasons.append("question_attribution_uncertain")
            result.append(_unresolved_region(raw_region, reasons))
            continue
        source_visual_bbox = transform_bbox_to_source(raw_region.visual_bbox, page.transform)

        target_matches: list[SourceRegion] = []
        foreign_matches: list[SourceRegion] = []
        shared_asset = False
        target_asset_ids = {
            ref.asset_id
            for region in target.source_regions
            for ref in region.asset_refs
            if ref.asset_id is not None
        }
        for answer in all_answers:
            for source_region in answer.source_regions:
                if source_region.page_index != raw_region.page_index:
                    continue
                if not _boxes_match(source_visual_bbox, source_region.bbox):
                    continue
                if source_region.content_pointer not in atoms:
                    continue
                atom = atoms[source_region.content_pointer]
                if atom.block.block_id != source_region.source_block_id:
                    continue
                refs = {
                    ref.asset_id
                    for ref in source_region.asset_refs
                    if ref.asset_id is not None
                }
                if answer.question_id == target.question_id:
                    target_matches.append(source_region)
                    if refs.intersection(
                        ref.asset_id
                        for other in all_answers
                        if other.question_id != target.question_id
                        for region in other.source_regions
                        for ref in region.asset_refs
                        if ref.asset_id is not None
                    ):
                        shared_asset = True
                else:
                    foreign_matches.append(source_region)
                    if refs.intersection(target_asset_ids):
                        shared_asset = True

        unique_target = {
            (region.content_pointer, region.source_block_id): region for region in target_matches
        }
        if len(unique_target) == 1 and not foreign_matches and not shared_asset:
            source = next(iter(unique_target.values()))
            atom = atoms[source.content_pointer]
            # The pointer is generated from the verified alignment and checked against the tree.
            if (
                source.content_pointer in target_ids
                and atom.block.page_index == raw_region.page_index
            ):
                result.append(
                    VisualEvidenceRegion(
                        page_index=raw_region.page_index,
                        visual_bbox=raw_region.visual_bbox,
                        evidence_type=raw_region.evidence_type,
                        candidate_content_pointer=source.content_pointer,
                        candidate_source_block_id=source.source_block_id,
                        attribution_status="candidate",
                        reason_codes=[*reasons, "visual_evidence_candidate_only"],
                    )
                )
                continue
        if foreign_matches or shared_asset:
            reasons.extend(("question_attribution_uncertain", "shared_figure_possible"))
        elif len(unique_target) != 1:
            reasons.append("source_mapping_unavailable")
        result.append(_unresolved_region(raw_region, reasons))
    return result


def _unresolved_region(raw_region, reasons: list[str]) -> VisualEvidenceRegion:
    safe_reasons = list(dict.fromkeys(code for code in reasons if code in VISUAL_REASON_CODES))
    return VisualEvidenceRegion(
        page_index=raw_region.page_index,
        visual_bbox=raw_region.visual_bbox,
        evidence_type=raw_region.evidence_type,
        attribution_status="unresolved",
        reason_codes=safe_reasons,
    )


def _boxes_match(
    visual_bbox: tuple[float, float, float, float],
    source_bbox: tuple[float, float, float, float] | None,
) -> bool:
    if source_bbox is None:
        return False
    vx0, vy0, vx1, vy1 = visual_bbox
    sx0, sy0, sx1, sy1 = source_bbox
    width = max(0.0, min(vx1, sx1) - max(vx0, sx0))
    height = max(0.0, min(vy1, sy1) - max(vy0, sy0))
    intersection = width * height
    union = (vx1 - vx0) * (vy1 - vy0) + (sx1 - sx0) * (sy1 - sy0) - intersection
    return union > 0 and intersection / union >= 0.08


async def _lookup_request_safely(
    session_factory: repository.SessionFactory,
    *,
    submission_id: UUID,
    question_id: UUID,
    request_id: UUID,
) -> VisualEvidenceArtifact | None:
    try:
        async with session_factory() as session:
            return await repository.get_request(
                session,
                submission_id=submission_id,
                question_id=question_id,
                request_id=request_id,
            )
    except Exception as error:
        logger.warning("Visual proposal commit status is unknown (%s)", type(error).__name__)
        return None


async def _delete_unindexed_object(
    storage: S3ObjectStorage,
    session_factory: repository.SessionFactory,
    object_key: str,
) -> None:
    try:
        async with session_factory() as session:
            indexed = await repository.is_object_indexed(
                session,
                bucket=storage.bucket,
                object_key=object_key,
            )
    except Exception as error:
        logger.warning("Visual proposal orphan check unavailable (%s)", type(error).__name__)
        return
    if indexed:
        return
    try:
        await storage.delete(object_key)
    except Exception as error:
        logger.warning("Visual proposal orphan cleanup failed (%s)", type(error).__name__)


def _proposal_object_key(
    prefix: str,
    submission_id: UUID,
    question_id: UUID,
    proposal_id: UUID,
) -> str:
    pieces = [
        prefix.strip("/"),
        "visual-evidence",
        str(submission_id),
        str(question_id),
        f"{proposal_id}.json",
    ]
    return "/".join(piece for piece in pieces if piece)
