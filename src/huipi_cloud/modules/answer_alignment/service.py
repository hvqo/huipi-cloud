"""Use cases for reliable, review-first alignment and bounded API projections."""

import hashlib
import json
import logging
from io import BytesIO
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.core.config import Settings, settings
from huipi_cloud.infrastructure.storage.s3 import (
    S3ObjectStorage,
    StorageObjectNotFoundError,
    StorageUnavailableError,
)
from huipi_cloud.modules.answer_alignment import repository
from huipi_cloud.modules.answer_alignment.errors import (
    AnswerAlignmentArtifactCorruptError,
    AnswerAlignmentLimitError,
    AnswerAlignmentNotFoundError,
    AnswerAlignmentNotReadyError,
    AnswerAlignmentResponseTooLargeError,
    AnswerAlignmentResultNotFoundError,
)
from huipi_cloud.modules.answer_alignment.matcher import assignment_questions_digest
from huipi_cloud.modules.answer_alignment.models import AnswerAlignmentArtifact
from huipi_cloud.modules.answer_alignment.protocol import (
    ALIGNER_VERSION,
    ALIGNMENT_SCHEMA_VERSION,
    AlignmentSummaryRead,
    AnswerAlignmentDocument,
    QuestionAlignmentRead,
)
from huipi_cloud.modules.answer_alignment.repository import AlignmentContext
from huipi_cloud.modules.answer_alignment.segmenter import align_canonical_document
from huipi_cloud.modules.canonical_documents.protocol import CanonicalDocument

logger = logging.getLogger(__name__)
_READ_CHUNK_SIZE = 64 * 1024


async def align_submission(
    session_factory: repository.SessionFactory,
    storage: S3ObjectStorage,
    submission_id: UUID,
    *,
    config: Settings = settings,
) -> AnswerAlignmentArtifact:
    """Align one Submission and persist immutable S3 JSON plus a PG index."""

    async with session_factory() as session:
        context = await repository.get_context(session, submission_id)
    if context is None:
        raise AnswerAlignmentNotFoundError("提交记录不存在")
    canonical = _require_canonical(context, storage)
    questions_digest = assignment_questions_digest(context.questions)
    alignment_id = uuid5(
        canonical.id,
        f"{ALIGNMENT_SCHEMA_VERSION}:{ALIGNER_VERSION}:{questions_digest}",
    )

    async with session_factory() as session:
        existing = await repository.get_artifact(
            session,
            canonical_artifact_id=canonical.id,
            aligner_version=ALIGNER_VERSION,
            questions_digest=questions_digest,
        )
    if existing is not None:
        return existing

    canonical_body = await _read_object(
        storage,
        canonical.canonical_object_key or "",
        expected_size=canonical.canonical_size_bytes,
        expected_sha256=canonical.canonical_sha256,
        maximum=config.canonical_max_document_bytes,
    )
    canonical_document = _decode_canonical_document(canonical_body, context)
    document = align_canonical_document(
        canonical_document,
        context.questions,
        assignment_id=context.assignment_id,
        canonical_sha256=canonical.canonical_sha256,
        alignment_id=alignment_id,
    )
    payload = json.dumps(
        document.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(payload) > config.answer_alignment_max_document_bytes:
        raise AnswerAlignmentLimitError("alignment_document_too_large")

    object_key = _alignment_object_key(storage.object_key_prefix, submission_id, alignment_id)
    try:
        await storage.upload_fileobj(BytesIO(payload), object_key, "application/json")
        metadata = await storage.head_object(object_key)
        if metadata.get("content_length") != len(payload):
            raise StorageUnavailableError("alignment object size did not match upload")
    except Exception:
        await _delete_orphan(storage, object_key, "upload_not_indexed")
        raise

    aligned_count = sum(answer.matching_status == "aligned" for answer in document.answers)
    review_count = sum(answer.matching_status == "review_required" for answer in document.answers)
    not_observed_count = sum(
        answer.matching_status == "not_observed" for answer in document.answers
    )
    unmatched_count = sum(
        candidate.matching_status == "unmatched" for candidate in document.candidates
    )
    try:
        record = await repository.register_success(
            session_factory,
            artifact_id=alignment_id,
            submission_id=submission_id,
            assignment_id=context.assignment_id,
            canonical_artifact_id=canonical.id,
            canonical_sha256=canonical.canonical_sha256,
            aligner_version=ALIGNER_VERSION,
            questions_digest=questions_digest,
            status=document.status,
            bucket=storage.bucket,
            object_key=object_key,
            alignment_sha256=hashlib.sha256(payload).hexdigest(),
            alignment_size_bytes=len(payload),
            question_count=len(document.answers),
            aligned_count=aligned_count,
            review_required_count=review_count,
            unmatched_count=unmatched_count,
            not_observed_count=not_observed_count,
        )
    except Exception as error:
        # The COMMIT result can be unknown after a connection failure. Keep the
        # immutable object so we never delete data that a committed index uses.
        logger.warning(
            "Answer Alignment registration failed; object may need reconciliation (%s)",
            type(error).__name__,
        )
        raise

    if record.object_key != object_key:
        # register_success returned only after its transaction committed. This
        # execution lost the idempotency race, so its unique run object is unused.
        await _delete_orphan(storage, object_key, "duplicate_alignment_run")
    return record


async def get_summary(
    session: AsyncSession,
    submission_id: UUID,
    *,
    storage: S3ObjectStorage,
) -> AlignmentSummaryRead:
    context = await repository.get_context(session, submission_id)
    if context is None:
        raise AnswerAlignmentNotFoundError("提交记录不存在")
    canonical = _require_canonical(context, storage)
    digest = assignment_questions_digest(context.questions)
    record = await repository.get_artifact(
        session,
        canonical_artifact_id=canonical.id,
        aligner_version=ALIGNER_VERSION,
        questions_digest=digest,
    )
    if record is None:
        return AlignmentSummaryRead(
            submission_id=submission_id,
            assignment_id=context.assignment_id,
            status="not_generated",
            canonical_document_id=canonical.id,
            question_count=len(context.questions),
            aligned_count=0,
            review_required_count=0,
            unmatched_count=0,
            not_observed_count=0,
        )
    return _summary(record, canonical.id)


async def get_question_alignment(
    session: AsyncSession,
    storage: S3ObjectStorage,
    submission_id: UUID,
    question_id: UUID,
    *,
    config: Settings = settings,
) -> QuestionAlignmentRead:
    context = await repository.get_context(session, submission_id)
    if context is None:
        raise AnswerAlignmentNotFoundError("提交记录不存在")
    canonical = _require_canonical(context, storage)
    question = next((item for item in context.questions if item.id == question_id), None)
    if question is None:
        raise AnswerAlignmentNotFoundError("题目不存在")
    digest = assignment_questions_digest(context.questions)
    record = await repository.get_artifact(
        session,
        canonical_artifact_id=canonical.id,
        aligner_version=ALIGNER_VERSION,
        questions_digest=digest,
    )
    if record is None:
        raise AnswerAlignmentResultNotFoundError("答案对齐结果尚未生成")
    if record.bucket != storage.bucket:
        raise StorageUnavailableError("alignment bucket does not match configured storage")
    payload = await _read_object(
        storage,
        record.object_key,
        expected_size=record.alignment_size_bytes,
        expected_sha256=record.alignment_sha256,
        maximum=config.answer_alignment_max_document_bytes,
        corrupt_on_mismatch=True,
    )
    document = _decode_alignment_document(payload, record, context, digest)
    answer = next((item for item in document.answers if item.question_id == question_id), None)
    if answer is None:
        raise AnswerAlignmentArtifactCorruptError("alignment question index is incomplete")
    response = QuestionAlignmentRead(
        submission_id=submission_id,
        assignment_id=context.assignment_id,
        canonical_document_id=canonical.id,
        answer=answer,
    )
    response_size = len(response.model_dump_json().encode("utf-8"))
    if response_size > config.answer_alignment_max_question_response_bytes:
        raise AnswerAlignmentResponseTooLargeError("question_alignment_response_too_large")
    return response


def _require_canonical(context: AlignmentContext, storage: S3ObjectStorage):
    canonical = context.canonical_artifact
    if canonical is None or canonical.status != "available":
        raise AnswerAlignmentNotReadyError("可用的规范化文档尚未生成")
    if context.parsing_status != "succeeded":
        raise AnswerAlignmentNotReadyError("解析任务尚未成功")
    if canonical.bucket != storage.bucket:
        raise StorageUnavailableError("canonical bucket does not match configured storage")
    if (
        canonical.canonical_object_key is None
        or canonical.canonical_sha256 is None
        or canonical.canonical_size_bytes is None
        or canonical.page_count is None
        or canonical.block_count is None
    ):
        raise AnswerAlignmentArtifactCorruptError("canonical index is incomplete")
    return canonical


async def _read_object(
    storage: S3ObjectStorage,
    object_key: str,
    *,
    expected_size: int,
    expected_sha256: str,
    maximum: int,
    corrupt_on_mismatch: bool = False,
) -> bytes:
    if expected_size < 1 or expected_size > maximum:
        raise AnswerAlignmentLimitError("alignment_source_size_limit_exceeded")
    try:
        downloaded = await storage.download(object_key)
    except StorageObjectNotFoundError as error:
        if corrupt_on_mismatch:
            raise AnswerAlignmentArtifactCorruptError("alignment object is missing") from error
        raise AnswerAlignmentArtifactCorruptError("canonical object is missing") from error
    digest = hashlib.sha256()
    payload = bytearray()
    async for chunk in downloaded.chunks(_READ_CHUNK_SIZE):
        if len(payload) + len(chunk) > maximum or len(payload) + len(chunk) > expected_size:
            raise AnswerAlignmentArtifactCorruptError("indexed object size does not match")
        payload.extend(chunk)
        digest.update(chunk)
    if len(payload) != expected_size or digest.hexdigest() != expected_sha256:
        raise AnswerAlignmentArtifactCorruptError("indexed object checksum does not match")
    return bytes(payload)


def _decode_canonical_document(payload: bytes, context: AlignmentContext) -> CanonicalDocument:
    canonical = context.canonical_artifact
    assert canonical is not None
    try:
        document = CanonicalDocument.model_validate_json(payload)
    except (ValidationError, ValueError, TypeError, RecursionError) as error:
        raise AnswerAlignmentArtifactCorruptError("canonical document is invalid") from error
    if (
        document.document_id != canonical.id
        or document.submission_id != context.submission_id
        or document.source_artifact_id != canonical.parsed_artifact_id
        or document.schema_version != canonical.schema_version
        or document.source_sha256 != canonical.source_sha256
        or document.page_count != canonical.page_count
        or sum(len(page.blocks) for page in document.pages) != canonical.block_count
        or hashlib.sha256(payload).hexdigest() != canonical.canonical_sha256
    ):
        raise AnswerAlignmentArtifactCorruptError("canonical index does not match object")
    return document


def _decode_alignment_document(
    payload: bytes,
    record: AnswerAlignmentArtifact,
    context: AlignmentContext,
    questions_digest: str,
) -> AnswerAlignmentDocument:
    try:
        document = AnswerAlignmentDocument.model_validate_json(payload)
    except (ValidationError, ValueError, TypeError, RecursionError) as error:
        raise AnswerAlignmentArtifactCorruptError("alignment document is invalid") from error
    if (
        document.alignment_id != record.id
        or document.submission_id != context.submission_id
        or document.assignment_id != context.assignment_id
        or document.canonical_document_id != record.canonical_artifact_id
        or document.canonical_sha256 != record.canonical_sha256
        or document.aligner_version != record.aligner_version
        or document.assignment_questions_digest != questions_digest
        or document.status != record.status
        or len(document.answers) != record.question_count
        or hashlib.sha256(payload).hexdigest() != record.alignment_sha256
    ):
        raise AnswerAlignmentArtifactCorruptError("alignment index does not match object")
    return document


def _summary(record: AnswerAlignmentArtifact, canonical_document_id: UUID) -> AlignmentSummaryRead:
    return AlignmentSummaryRead(
        submission_id=record.submission_id,
        assignment_id=record.assignment_id,
        status=record.status,
        canonical_document_id=canonical_document_id,
        aligner_version=record.aligner_version,
        assignment_questions_digest=record.assignment_questions_digest,
        question_count=record.question_count,
        aligned_count=record.aligned_count,
        review_required_count=record.review_required_count,
        unmatched_count=record.unmatched_count,
        not_observed_count=record.not_observed_count,
    )


async def _delete_orphan(storage: S3ObjectStorage, object_key: str, reason: str) -> None:
    try:
        await storage.delete(object_key)
    except Exception as error:
        logger.warning(
            "Answer Alignment object cleanup failed (%s, %s)",
            reason,
            type(error).__name__,
        )


def _alignment_object_key(prefix: str, submission_id: UUID, artifact_id: UUID) -> str:
    run_id = uuid4()
    return "/".join(
        part
        for part in (
            prefix.strip("/"),
            "answer-alignment",
            str(submission_id),
            "artifacts",
            str(artifact_id),
            "aligners",
            ALIGNER_VERSION,
            "runs",
            str(run_id),
            "answer_alignment.json",
        )
        if part
    )
