"""Rubric-based LLM proposals over verified immutable GradeInput artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from decimal import Decimal
from io import BytesIO
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.core.config import Settings, settings
from huipi_cloud.infrastructure.database.base import utc_now
from huipi_cloud.infrastructure.grading.provider import (
    TextGradingProvider,
    build_text_grading_provider,
)
from huipi_cloud.infrastructure.storage.s3 import (
    S3ObjectStorage,
    StorageUnavailableError,
)
from huipi_cloud.modules.grading import proposal_repository
from huipi_cloud.modules.grading.errors import (
    GradeInputArtifactCorruptError,
    GradeInputCommitOutcomeUnknownError,
    GradeInputError,
    GradeInputNotFoundError,
    GradeInputSourceChangedError,
)
from huipi_cloud.modules.grading.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from huipi_cloud.modules.grading.proposal_errors import (
    GradeProposalArtifactCorruptError,
    GradeProposalCommitOutcomeUnknownError,
    GradeProposalError,
    GradeProposalIdempotencyConflictError,
    GradeProposalInputStaleError,
    GradeProposalInputTooLargeError,
    GradeProposalInvalidEvidenceError,
    GradeProposalInvalidModelResponseError,
    GradeProposalInvalidRubricScoresError,
    GradeProposalNotFoundError,
)
from huipi_cloud.modules.grading.proposal_protocol import (
    CriterionScore,
    CriterionScoreDraft,
    GradeModelReply,
    GradeProposalDocument,
    GradeProposalInspection,
    GradeProposalRunResult,
    NoEvidenceReference,
    QuoteEvidenceReference,
    proposal_json_bytes,
)
from huipi_cloud.modules.grading.protocol import (
    GradeInputDocument,
    json_sha256,
)
from huipi_cloud.modules.grading.service import read_grade_input_artifact

SessionFactory = async_sessionmaker[AsyncSession]
logger = logging.getLogger(__name__)
_PROMPT_TOKEN_WRAPPER_ALLOWANCE = 256


async def inspect_grade_proposal_input(
    session_factory: SessionFactory,
    storage: S3ObjectStorage,
    *,
    grade_input_artifact_id: UUID,
    config: Settings = settings,
    provider: TextGradingProvider | None = None,
) -> GradeProposalInspection:
    selected_provider = provider or build_text_grading_provider(config)
    document = await _read_current_input(
        session_factory,
        storage,
        grade_input_artifact_id,
        config=config,
    )
    user_prompt = build_user_prompt(document)
    _check_prompt_budget(SYSTEM_PROMPT, user_prompt, config)
    return GradeProposalInspection(
        status="ready",
        grade_input_artifact_id=document.artifact_id,
        grade_input_sha256=document.input_sha256,
        submission_id=document.submission_id,
        question_id=document.question.question_id,
        question_number=document.question.question_number,
        rubric_criterion_count=len(document.rubric_snapshot),
        rubric_total_points=document.question.max_score,
        provider=selected_provider.provider_name,
        model_id=selected_provider.model_id,
        prompt_version=PROMPT_VERSION,
        max_input_bytes=config.grading_max_input_bytes,
        max_input_tokens=config.grading_max_input_tokens,
        max_output_tokens=config.grading_max_output_tokens,
        max_attempts=1 + config.grading_invalid_response_retries,
    )


async def grade_input_artifact(
    session_factory: SessionFactory,
    storage: S3ObjectStorage,
    *,
    grade_input_artifact_id: UUID,
    request_id: UUID,
    config: Settings = settings,
    provider: TextGradingProvider | None = None,
) -> GradeProposalRunResult:
    """Call one configured provider, validate its result, and persist a proposal."""

    selected_provider = provider or build_text_grading_provider(config)
    document = await _read_current_input(
        session_factory,
        storage,
        grade_input_artifact_id,
        config=config,
    )
    base_prompt = build_user_prompt(document)
    _check_prompt_budget(SYSTEM_PROMPT, base_prompt, config)
    request_fingerprint = _request_fingerprint(document, selected_provider)

    existing = await proposal_repository.get_by_request_id(session_factory, request_id)
    if existing is not None:
        if (
            existing.request_fingerprint != request_fingerprint
            or existing.grade_input_artifact_id != document.artifact_id
            or existing.grade_input_sha256 != document.input_sha256
        ):
            raise GradeProposalIdempotencyConflictError("request_id_payload_conflict")
        stored_document, stored_sha = await read_grade_proposal_artifact(
            session_factory,
            storage,
            existing.id,
            config=config,
        )
        return _result(stored_document, stored_sha, created=False)

    validated_reply, response_model, usage_counts, elapsed_ms = await _call_and_validate(
        selected_provider,
        document,
        config=config,
    )

    # Re-read after the model call. The repository repeats source validation while
    # holding short row locks in the transaction that writes the index.
    current_document = await _read_current_input(
        session_factory,
        storage,
        grade_input_artifact_id,
        config=config,
    )
    if current_document.input_sha256 != document.input_sha256:
        raise GradeProposalInputStaleError("grade_input_changed_during_model_call")

    proposal_id = uuid4()
    if validated_reply.outcome == "abstain":
        final_scores: list[CriterionScore] = []
        total_score = None
        grading_status = "grading_abstained"
    else:
        final_scores, total_score = _validate_candidate_scores(validated_reply, document, config)
        grading_status = "completed_candidate"

    proposal = GradeProposalDocument(
        proposal_id=proposal_id,
        request_id=request_id,
        request_fingerprint=request_fingerprint,
        grade_input_artifact_id=document.artifact_id,
        grade_input_sha256=document.input_sha256,
        submission_id=document.submission_id,
        question_id=document.question.question_id,
        model_id=response_model,
        provider=selected_provider.provider_name,
        prompt_version=PROMPT_VERSION,
        generation_config_digest=selected_provider.generation_config_digest,
        criterion_scores=final_scores,
        total_score=total_score,
        max_score=document.question.max_score,
        grading_status=grading_status,
        reason_codes=validated_reply.reason_codes,
        abstention_reason=validated_reply.abstention_reason,
        prompt_tokens=usage_counts[0],
        completion_tokens=usage_counts[1],
        model_elapsed_ms=elapsed_ms,
        created_at=utc_now(),
    )
    payload = proposal_json_bytes(proposal)
    if len(payload) > config.grading_max_proposal_bytes:
        raise GradeProposalInvalidModelResponseError("proposal_size_limit_exceeded")
    object_sha = hashlib.sha256(payload).hexdigest()
    object_key = _proposal_object_key(storage, document, request_id, proposal_id)

    try:
        await storage.upload_fileobj(BytesIO(payload), object_key, "application/json")
    except asyncio.CancelledError:
        # S3 I/O can continue in boto3's worker thread. The attempt key is unique;
        # retain it instead of racing a delete against the in-flight upload.
        raise
    except Exception:
        await _best_effort_delete(storage, object_key, "upload_failed")
        raise

    try:
        record, created = await proposal_repository.register_proposal(
            session_factory,
            input_document=document,
            proposal=proposal,
            bucket=storage.bucket,
            object_key=object_key,
            object_sha256=object_sha,
            object_size_bytes=len(payload),
        )
    except GradeProposalCommitOutcomeUnknownError:
        logger.warning("GradeProposal commit outcome is unknown; preserving attempt object")
        raise
    except GradeInputCommitOutcomeUnknownError:
        logger.warning("GradeInput validation commit outcome is unknown; preserving proposal")
        raise GradeProposalCommitOutcomeUnknownError from None
    except GradeProposalError:
        await _best_effort_delete(storage, object_key, "registration_not_committed")
        raise
    except GradeInputError as error:
        await _best_effort_delete(storage, object_key, "grade_input_source_not_current")
        raise GradeProposalInputStaleError("grade_input_source_not_current") from error
    except DBAPIError as error:
        await _best_effort_delete(storage, object_key, "database_registration_failed")
        logger.error("GradeProposal database registration failed (%s)", type(error).__name__)
        raise

    if record.bucket != storage.bucket or record.object_key != object_key:
        await _best_effort_delete(storage, object_key, "duplicate_request_loser")
        winner, winner_sha = await _read_proposal_from_record(
            storage,
            record,
            maximum=config.grading_max_proposal_bytes,
        )
        return _result(winner, winner_sha, created=False)
    return _result(proposal, object_sha, created=created)


async def read_grade_proposal_artifact(
    session_factory: SessionFactory,
    storage: S3ObjectStorage,
    proposal_id: UUID,
    *,
    config: Settings = settings,
) -> tuple[GradeProposalDocument, str]:
    async with session_factory() as session:
        record = await proposal_repository.get_proposal(session, proposal_id)
        if record is None:
            raise GradeProposalNotFoundError("proposal_not_found")

    input_document = await _read_current_input(
        session_factory,
        storage,
        record.grade_input_artifact_id,
        config=config,
    )
    if (
        record.grade_input_sha256 != input_document.input_sha256
        or record.submission_id != input_document.submission_id
        or record.question_id != input_document.question.question_id
    ):
        raise GradeProposalInputStaleError("proposal_input_no_longer_current")
    return await _read_proposal_from_record(
        storage,
        record,
        maximum=config.grading_max_proposal_bytes,
    )


async def _read_current_input(
    session_factory: SessionFactory,
    storage: S3ObjectStorage,
    artifact_id: UUID,
    *,
    config: Settings,
) -> GradeInputDocument:
    try:
        return await read_grade_input_artifact(
            session_factory,
            storage,
            artifact_id,
            config=config,
        )
    except GradeInputNotFoundError as error:
        raise GradeProposalNotFoundError("grade_input_not_found") from error
    except GradeInputSourceChangedError as error:
        raise GradeProposalInputStaleError(error.reason_code) from error
    except GradeInputArtifactCorruptError as error:
        raise GradeProposalInputStaleError("grade_input_integrity_failed") from error


async def _call_and_validate(
    provider: TextGradingProvider,
    document: GradeInputDocument,
    *,
    config: Settings,
) -> tuple[GradeModelReply, str, tuple[int | None, int | None], int]:
    retry_reason: str | None = None
    prompt_tokens = 0
    completion_tokens = 0
    prompt_tokens_complete = True
    completion_tokens_complete = True
    elapsed_ms = 0
    response_model = provider.model_id

    for attempt in range(config.grading_invalid_response_retries + 1):
        user_prompt = build_user_prompt(document, retry_reason=retry_reason)
        _check_prompt_budget(SYSTEM_PROMPT, user_prompt, config)
        response = await provider.complete(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt)
        elapsed_ms += response.elapsed_ms
        response_model = response.model_id
        if response.usage.prompt_tokens is None:
            prompt_tokens_complete = False
        else:
            prompt_tokens += response.usage.prompt_tokens
        if response.usage.completion_tokens is None:
            completion_tokens_complete = False
        else:
            completion_tokens += response.usage.completion_tokens
        try:
            reply = GradeModelReply.model_validate_json(response.content)
        except ValidationError as error:
            classified = _classify_reply_validation_error(error)
            if (
                isinstance(classified, GradeProposalInvalidModelResponseError)
                and attempt < config.grading_invalid_response_retries
            ):
                retry_reason = classified.reason_code
                continue
            raise classified from error
        return (
            reply,
            response_model,
            (
                prompt_tokens if prompt_tokens_complete else None,
                completion_tokens if completion_tokens_complete else None,
            ),
            elapsed_ms,
        )
    raise GradeProposalInvalidModelResponseError("model_response_retry_exhausted")


def _classify_reply_validation_error(error: ValidationError) -> GradeProposalError:
    locations = {str(part) for detail in error.errors() for part in detail.get("loc", ())}
    if locations & {"segment_index", "text_start", "text_end", "quote", "reason"}:
        return GradeProposalInvalidEvidenceError("model_evidence_protocol_invalid")
    if locations & {"awarded_points", "total_score"}:
        return GradeProposalInvalidRubricScoresError("model_score_protocol_invalid")
    return GradeProposalInvalidModelResponseError("model_json_contract_invalid")


def _validate_candidate_scores(
    reply: GradeModelReply,
    document: GradeInputDocument,
    config: Settings,
) -> tuple[list[CriterionScore], Decimal]:
    expected = {item.criterion_id: item for item in document.rubric_snapshot}
    drafts: dict[UUID, CriterionScoreDraft] = {}
    for draft in reply.criterion_scores:
        if draft.criterion_id not in expected or draft.criterion_id in drafts:
            raise GradeProposalInvalidRubricScoresError("rubric_criterion_unknown_or_duplicate")
        drafts[draft.criterion_id] = draft
    if set(drafts) != set(expected):
        raise GradeProposalInvalidRubricScoresError("rubric_criterion_missing")

    validated: list[CriterionScore] = []
    for criterion in document.rubric_snapshot:
        draft = drafts[criterion.criterion_id]
        if draft.awarded_points < 0 or draft.awarded_points > criterion.points:
            raise GradeProposalInvalidRubricScoresError("criterion_score_out_of_range")
        if len(draft.rationale) > config.grading_max_rationale_characters:
            raise GradeProposalInvalidRubricScoresError("criterion_rationale_too_long")
        has_valid_quote = False
        for evidence in draft.evidence_references:
            if isinstance(evidence, QuoteEvidenceReference):
                _validate_quote_reference(evidence, document, config)
                has_valid_quote = True
            elif not isinstance(evidence, NoEvidenceReference):
                raise GradeProposalInvalidEvidenceError("evidence_reference_kind_invalid")
        if draft.awarded_points > 0 and not has_valid_quote:
            raise GradeProposalInvalidEvidenceError("positive_score_without_quote")
        validated.append(
            CriterionScore(
                criterion_id=criterion.criterion_id,
                awarded_points=draft.awarded_points,
                max_points=criterion.points,
                rationale=draft.rationale,
                evidence_references=draft.evidence_references,
            )
        )

    calculated = sum((item.awarded_points for item in validated), Decimal("0.00"))
    if calculated > document.question.max_score:
        raise GradeProposalInvalidRubricScoresError("question_score_out_of_range")
    if reply.total_score != calculated:
        raise GradeProposalInvalidRubricScoresError("model_total_mismatch")
    return validated, calculated


def _validate_quote_reference(
    evidence: QuoteEvidenceReference,
    document: GradeInputDocument,
    config: Settings,
) -> None:
    if len(evidence.quote) > config.grading_max_quote_characters:
        raise GradeProposalInvalidEvidenceError("evidence_quote_too_long")
    if evidence.segment_index >= len(document.selected_answer_segments):
        raise GradeProposalInvalidEvidenceError("evidence_segment_not_found")
    segment = document.selected_answer_segments[evidence.segment_index]
    if evidence.text_end > len(segment.text):
        raise GradeProposalInvalidEvidenceError("evidence_range_out_of_bounds")
    if segment.text[evidence.text_start : evidence.text_end] != evidence.quote:
        raise GradeProposalInvalidEvidenceError("evidence_quote_mismatch")


def _check_prompt_budget(system_prompt: str, user_prompt: str, config: Settings) -> None:
    byte_count = len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))
    if byte_count > config.grading_max_input_bytes:
        raise GradeProposalInputTooLargeError("grading_prompt_byte_limit_exceeded")
    # UTF-8 bytes form a conservative text-token upper bound; include bounded
    # chat framing overhead because this adapter does not ship a model tokenizer.
    if byte_count + _PROMPT_TOKEN_WRAPPER_ALLOWANCE > config.grading_max_input_tokens:
        raise GradeProposalInputTooLargeError("grading_prompt_token_budget_exceeded")


def _request_fingerprint(
    document: GradeInputDocument,
    provider: TextGradingProvider,
) -> str:
    return json_sha256(
        {
            "grade_input_artifact_id": str(document.artifact_id),
            "grade_input_sha256": document.input_sha256,
            "provider": provider.provider_name,
            "model_id": provider.model_id,
            "prompt_version": PROMPT_VERSION,
            "generation_config_digest": provider.generation_config_digest,
        }
    )


def _proposal_object_key(
    storage: S3ObjectStorage,
    document: GradeInputDocument,
    request_id: UUID,
    proposal_id: UUID,
) -> str:
    relative = (
        f"grading/proposals/{document.artifact_id}/{request_id}/{proposal_id}.json"
    )
    prefix = storage.object_key_prefix.strip("/")
    return f"{prefix}/{relative}" if prefix else relative


async def _read_proposal_from_record(
    storage: S3ObjectStorage,
    record,
    *,
    maximum: int,
) -> tuple[GradeProposalDocument, str]:
    if record.bucket != storage.bucket:
        raise StorageUnavailableError("proposal bucket does not match configured storage")
    try:
        downloaded = await storage.download(record.object_key)
        payload = bytearray()
        async for chunk in downloaded.chunks():
            if len(payload) + len(chunk) > maximum:
                raise GradeProposalArtifactCorruptError("proposal_size_limit_exceeded")
            payload.extend(chunk)
    except StorageUnavailableError:
        raise
    body = bytes(payload)
    digest = hashlib.sha256(body).hexdigest()
    if len(body) != record.object_size_bytes or digest != record.object_sha256:
        raise GradeProposalArtifactCorruptError("proposal_object_digest_mismatch")
    try:
        proposal = GradeProposalDocument.model_validate_json(body)
    except ValidationError as error:
        raise GradeProposalArtifactCorruptError("proposal_protocol_invalid") from error
    if (
        proposal.proposal_id != record.id
        or proposal.request_id != record.request_id
        or proposal.request_fingerprint != record.request_fingerprint
        or proposal.grade_input_artifact_id != record.grade_input_artifact_id
        or proposal.grade_input_sha256 != record.grade_input_sha256
        or proposal.submission_id != record.submission_id
        or proposal.question_id != record.question_id
        or proposal.provider != record.provider
        or proposal.model_id != record.model_id
        or proposal.prompt_version != record.prompt_version
        or proposal.generation_config_digest != record.generation_config_digest
        or proposal.grading_status != record.grading_status
        or proposal.prompt_tokens != record.prompt_tokens
        or proposal.completion_tokens != record.completion_tokens
        or proposal.model_elapsed_ms != record.model_elapsed_ms
    ):
        raise GradeProposalArtifactCorruptError("proposal_index_mismatch")
    return proposal, digest


def _result(
    proposal: GradeProposalDocument,
    object_sha256: str,
    *,
    created: bool,
) -> GradeProposalRunResult:
    return GradeProposalRunResult(
        status=proposal.grading_status,
        proposal_id=proposal.proposal_id,
        request_id=proposal.request_id,
        grade_input_artifact_id=proposal.grade_input_artifact_id,
        grade_input_sha256=proposal.grade_input_sha256,
        proposal_sha256=object_sha256,
        criterion_scores=[
            (item.criterion_id, item.awarded_points)
            for item in proposal.criterion_scores
        ],
        total_score=proposal.total_score,
        max_score=proposal.max_score,
        reason_codes=proposal.reason_codes,
        prompt_tokens=proposal.prompt_tokens,
        completion_tokens=proposal.completion_tokens,
        model_elapsed_ms=proposal.model_elapsed_ms,
        created=created,
    )


async def _best_effort_delete(storage: S3ObjectStorage, object_key: str, reason: str) -> None:
    try:
        await storage.delete(object_key)
    except Exception as error:
        logger.warning(
            "GradeProposal object cleanup failed (%s, %s)",
            reason,
            type(error).__name__,
        )
