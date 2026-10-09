"""Canonical document use cases, object integrity checks, and API projections."""

import hashlib
import json
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from uuid import UUID, uuid4, uuid5

from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.core.config import Settings, settings
from huipi_cloud.infrastructure.storage.s3 import (
    S3ObjectStorage,
    StorageObjectNotFoundError,
    StorageUnavailableError,
)
from huipi_cloud.modules.canonical_documents import repository
from huipi_cloud.modules.canonical_documents.errors import (
    CanonicalArtifactCorruptError,
    CanonicalDocumentFailedError,
    CanonicalDocumentNotFoundError,
    CanonicalDocumentNotReadyError,
    CanonicalNormalizationError,
    CanonicalPageNotFoundError,
)
from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact
from huipi_cloud.modules.canonical_documents.normalizer import (
    NormalizationSource,
    normalize_middle_json,
)
from huipi_cloud.modules.canonical_documents.protocol import (
    CANONICAL_SCHEMA_VERSION,
    NORMALIZER_VERSION,
    CanonicalDocument,
    CanonicalDocumentStatusRead,
    CanonicalDocumentSummary,
    CanonicalPageRead,
)

logger = logging.getLogger(__name__)
_READ_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class _Payload:
    data: bytearray
    sha256: str


async def normalize_submission(
    session_factory: repository.SessionFactory,
    storage: S3ObjectStorage,
    submission_id: UUID,
    *,
    config: Settings = settings,
) -> CanonicalArtifact:
    """Normalize a successful parse and persist immutable JSON plus one PG index."""
    source_record = await repository.get_source_for_normalization(
        session_factory,
        submission_id,
    )
    if source_record is None:
        raise CanonicalDocumentNotFoundError("提交记录不存在")
    parsed = source_record.parsed_artifact
    document_id = uuid5(
        parsed.id,
        f"{CANONICAL_SCHEMA_VERSION}:{NORMALIZER_VERSION}",
    )

    existing = await repository.get_current_canonical_artifact(session_factory, parsed.id)
    if existing is not None:
        return existing

    try:
        if parsed.bucket != storage.bucket:
            raise CanonicalNormalizationError("storage_bucket_mismatch")
        middle_payload = await _read_checked_object(
            storage,
            parsed.middle_json_key,
            expected_size=parsed.middle_json_size_bytes,
            expected_sha256=parsed.middle_json_sha256,
            maximum=config.canonical_max_middle_json_bytes,
        )
        manifest_payload = await _read_checked_object(
            storage,
            parsed.assets_manifest_key,
            expected_size=parsed.assets_manifest_size_bytes,
            expected_sha256=parsed.assets_manifest_sha256,
            maximum=config.canonical_max_manifest_bytes,
        )
        source = NormalizationSource(
            document_id=document_id,
            submission_id=source_record.submission.id,
            source_artifact_id=parsed.id,
            source_parser=parsed.parser_name,
            source_parser_version=parsed.parser_version,
            source_schema_name=parsed.schema_name,
            source_schema_version=parsed.schema_version,
            source_sha256=parsed.original_sha256,
            source_middle_json_sha256=parsed.middle_json_sha256,
            source_page_count=parsed.page_count,
            source_asset_count=parsed.asset_count,
            source_tier=parsed.tier,
        )
        document = normalize_middle_json(
            middle_payload.data,
            manifest_payload.data,
            source,
            max_nodes=config.canonical_max_nodes,
            max_nesting_depth=config.canonical_max_nesting_depth,
        )
        asset_keys = _asset_object_keys(manifest_payload.data, document)
        await _verify_referenced_assets(storage, document, asset_keys)
        body = json.dumps(
            document.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(body) > config.canonical_max_document_bytes:
            raise CanonicalNormalizationError("normalized_document_too_large")

        object_key = _canonical_object_key(storage.object_key_prefix, submission_id, parsed.id)
        await storage.upload_fileobj(BytesIO(body), object_key, "application/json")
        metadata = await storage.head_object(object_key)
        if metadata.get("content_length") != len(body):
            raise StorageUnavailableError("canonical object size did not match upload")
        return await repository.register_success(
            session_factory,
            document_id=document_id,
            source=source_record,
            bucket=storage.bucket,
            object_key=object_key,
            canonical_sha256=hashlib.sha256(body).hexdigest(),
            canonical_size_bytes=len(body),
            page_count=document.page_count,
            block_count=sum(len(page.blocks) for page in document.pages),
        )
    except CanonicalNormalizationError as error:
        try:
            await repository.register_failure(
                session_factory,
                document_id=document_id,
                source=source_record,
                bucket=parsed.bucket,
                failure_code=error.code,
            )
        except Exception as persistence_error:
            logger.error(
                "Canonical failure marker could not be persisted (%s)",
                type(persistence_error).__name__,
            )
            raise
        raise
    except StorageObjectNotFoundError as error:
        failure = CanonicalNormalizationError("source_artifact_missing")
        try:
            await repository.register_failure(
                session_factory,
                document_id=document_id,
                source=source_record,
                bucket=parsed.bucket,
                failure_code=failure.code,
            )
        except Exception as persistence_error:
            logger.error(
                "Canonical failure marker could not be persisted (%s)",
                type(persistence_error).__name__,
            )
            raise
        raise failure from error
    except StorageUnavailableError:
        try:
            await repository.register_failure(
                session_factory,
                document_id=document_id,
                source=source_record,
                bucket=parsed.bucket,
                failure_code="object_store_unavailable",
            )
        except Exception as persistence_error:
            logger.error(
                "Canonical storage failure marker could not be persisted (%s)",
                type(persistence_error).__name__,
            )
        raise


async def get_status(
    session: AsyncSession,
    submission_id: UUID,
) -> CanonicalDocumentStatusRead:
    """Return safe status metadata without loading the canonical object."""
    record = await repository.get_status_for_submission(session, submission_id)
    if record is None:
        raise CanonicalDocumentNotFoundError("提交记录不存在")
    canonical = record.canonical_artifact
    status_value = "not_generated"
    failure_code = None
    summary = None
    if canonical is not None and canonical.status == "failed":
        status_value = "failed"
        failure_code = canonical.failure_code
    elif canonical is not None and canonical.status == "available":
        status_value = "available"
        summary = _summary(canonical)
    return CanonicalDocumentStatusRead(
        submission_id=submission_id,
        status=status_value,
        source_parsing_status=record.task_status or "not_found",
        failure_code=failure_code,
        canonical_document=summary,
    )


async def get_page(
    session: AsyncSession,
    storage: S3ObjectStorage,
    submission_id: UUID,
    page_number: int,
    *,
    config: Settings = settings,
) -> CanonicalPageRead:
    """Read and verify one bounded Canonical JSON object, then return one page."""
    record = await repository.get_status_for_submission(session, submission_id)
    if record is None:
        raise CanonicalDocumentNotFoundError("提交记录不存在")
    canonical = record.canonical_artifact
    if canonical is None:
        raise CanonicalDocumentNotReadyError("规范化结果尚未生成")
    if canonical.status == "failed":
        raise CanonicalDocumentFailedError(canonical.failure_code or "normalization_failed")
    if canonical.bucket != storage.bucket:
        raise StorageUnavailableError("canonical bucket does not match configured storage")
    if canonical.canonical_size_bytes is None:
        raise CanonicalArtifactCorruptError("canonical index is incomplete")

    try:
        payload = await _read_checked_object(
            storage,
            canonical.canonical_object_key or "",
            expected_size=canonical.canonical_size_bytes,
            expected_sha256=canonical.canonical_sha256 or "",
            maximum=config.canonical_max_document_bytes,
        )
    except (CanonicalNormalizationError, StorageObjectNotFoundError) as error:
        raise CanonicalArtifactCorruptError(
            "canonical object failed integrity validation"
        ) from error
    try:
        document = CanonicalDocument.model_validate_json(payload.data)
    except (ValueError, TypeError, RecursionError) as error:
        raise CanonicalArtifactCorruptError("canonical document cannot be decoded") from error
    if (
        document.document_id != canonical.id
        or document.submission_id != submission_id
        or document.source_artifact_id != canonical.parsed_artifact_id
        or document.page_count != canonical.page_count
        or sum(len(page.blocks) for page in document.pages) != canonical.block_count
        or document.schema_version != canonical.schema_version
        or document.source_sha256 != canonical.source_sha256
        or hashlib.sha256(payload.data).hexdigest() != canonical.canonical_sha256
    ):
        raise CanonicalArtifactCorruptError("canonical index does not match object")
    if page_number < 1 or page_number > document.page_count:
        raise CanonicalPageNotFoundError("指定页码不存在")
    page = document.pages[page_number - 1]
    return CanonicalPageRead(
        document_id=document.document_id,
        submission_id=document.submission_id,
        source_artifact_id=document.source_artifact_id,
        schema_version=document.schema_version,
        bbox_coordinate_space=document.bbox_coordinate_space,
        page=page,
    )


async def _read_checked_object(
    storage: S3ObjectStorage,
    object_key: str,
    *,
    expected_size: int,
    expected_sha256: str,
    maximum: int,
) -> _Payload:
    if expected_size < 1 or expected_size > maximum:
        raise CanonicalNormalizationError("source_size_limit_exceeded")
    try:
        downloaded = await storage.download(object_key)
    except StorageObjectNotFoundError:
        raise
    digest = hashlib.sha256()
    data = bytearray()
    async for chunk in downloaded.chunks(_READ_CHUNK_SIZE):
        if len(data) + len(chunk) > maximum or len(data) + len(chunk) > expected_size:
            raise CanonicalNormalizationError("source_size_mismatch")
        data.extend(chunk)
        digest.update(chunk)
    actual_digest = digest.hexdigest()
    if len(data) != expected_size:
        raise CanonicalNormalizationError("source_size_mismatch")
    if actual_digest != expected_sha256:
        raise CanonicalNormalizationError("source_checksum_mismatch")
    return _Payload(data, actual_digest)


async def _verify_referenced_assets(
    storage: S3ObjectStorage,
    document: CanonicalDocument,
    asset_keys: dict[UUID, str],
) -> None:
    asset_ids = {
        reference.asset_id
        for page in document.pages
        for block in page.blocks
        for reference in _walk_references(block.content)
        if reference.kind == "stored" and reference.asset_id is not None
    }
    for asset in document.assets:
        if asset.asset_id not in asset_ids:
            continue
        key = asset_keys.get(asset.asset_id)
        if key is None:
            raise CanonicalNormalizationError("invalid_asset_manifest")
        try:
            metadata = await storage.head_object(key)
        except StorageObjectNotFoundError as error:
            raise CanonicalNormalizationError("source_asset_missing") from error
        if metadata.get("content_length") != asset.size_bytes:
            raise CanonicalNormalizationError("source_asset_invalid")


def _asset_object_keys(manifest_bytes: bytes, document: CanonicalDocument) -> dict[UUID, str]:
    """Resolve logical asset IDs to private keys only inside the storage boundary."""
    manifest = json.loads(manifest_bytes)
    path_to_key = {
        "/".join(part for part in PurePosixPath(item["path"]).parts if part not in {"", "."}): item[
            "object_key"
        ]
        for item in manifest["assets"]
    }
    return {
        asset.asset_id: path_to_key[asset.source_path]
        for asset in document.assets
        if asset.source_path in path_to_key
    }


def _summary(record: CanonicalArtifact) -> CanonicalDocumentSummary:
    if (
        record.canonical_object_key is None
        or record.canonical_sha256 is None
        or record.page_count is None
        or record.block_count is None
    ):
        raise CanonicalArtifactCorruptError("canonical index is incomplete")
    return CanonicalDocumentSummary(
        document_id=record.id,
        source_artifact_id=record.parsed_artifact_id,
        schema_version=record.schema_version,
        normalizer_version=record.normalizer_version,
        source_parser=record.source_parser,
        source_parser_version=record.source_parser_version,
        source_sha256=record.source_sha256,
        canonical_sha256=record.canonical_sha256,
        page_count=record.page_count,
        block_count=record.block_count,
        created_at=record.created_at,
    )


def _canonical_object_key(prefix: str, submission_id: UUID, artifact_id: UUID) -> str:
    run_id = uuid4()
    return "/".join(
        part
        for part in (
            prefix.strip("/"),
            "canonical",
            str(submission_id),
            "artifacts",
            str(artifact_id),
            "normalizers",
            NORMALIZER_VERSION,
            "runs",
            str(run_id),
            "canonical.json",
        )
        if part
    )


def _walk_references(node):
    yield from node.asset_refs
    for child in node.children:
        yield from _walk_references(child)
