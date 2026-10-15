"""Explicit teacher imports: immutable S3 objects, transactional version/chunk registration."""

import json
import logging
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.infrastructure.database.base import utc_now
from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.knowledge.chunking import chunk_text, decode_source, sha256
from huipi_cloud.modules.knowledge.models import KnowledgeChunk, KnowledgeDocument
from huipi_cloud.modules.knowledge.schemas import (
    CHUNKER_VERSION,
    MAX_ACTIVE_DOCUMENTS,
    MAX_SCOPE_CHUNKS,
    DocumentReceipt,
    ImportRequest,
    KnowledgeError,
)

log = logging.getLogger(__name__)


def receipt(row, *, reused):
    return DocumentReceipt(
        document_id=row.document_id,
        source_version=row.source_version,
        source_sha256=row.source_sha256,
        status=row.status,
        chunk_count=row.chunk_count,
        reused=reused,
    )


async def require_scope(session, assignment_id, document_id, *, lock=False):
    query = select(Assignment.id).where(Assignment.id == assignment_id)
    if lock:
        query = query.with_for_update()
    if await session.scalar(query) is None:
        raise KnowledgeError("knowledge_not_found_in_scope")
    owner = await session.scalar(
        select(KnowledgeDocument.assignment_id)
        .where(KnowledgeDocument.document_id == document_id)
        .limit(1)
    )
    if owner is not None and owner != assignment_id:
        raise KnowledgeError("knowledge_not_found_in_scope")


async def _delete_loser(storage, key):
    try:
        await storage.delete(key)
    except StorageUnavailableError:
        # No key, title, text, or credential in public logs.
        log.warning("knowledge_object_cleanup_pending")


async def import_material(factory, storage, request: ImportRequest, raw: bytes) -> DocumentReceipt:
    request = ImportRequest.model_validate(request.model_dump())
    value = decode_source(raw, request.source_sha256)
    chunks = chunk_text(value)
    document_id = request.document_id or uuid5(
        NAMESPACE_URL, f"huipi:teacher-material:{request.assignment_id}:{request.title}"
    )
    input_sha = sha256(
        json.dumps(
            {
                **request.model_dump(mode="json", exclude={"document_id"}),
                "chunker_version": CHUNKER_VERSION,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    )
    if factory is None:
        raise KnowledgeError("knowledge_database_unavailable")
    try:
        async with factory() as session:
            await require_scope(session, request.assignment_id, document_id)
            previous = await session.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.document_id == document_id,
                    KnowledgeDocument.input_sha256 == input_sha,
                )
            )
            if previous:
                return receipt(previous, reused=True)  # Never reactivate revoked/superseded data.
    except (SQLAlchemyError, OSError):
        raise KnowledgeError("knowledge_database_unavailable") from None
    if storage is None:
        raise KnowledgeError("knowledge_storage_not_configured")
    key = "/".join(
        filter(
            None,
            [
                storage.object_key_prefix,
                "knowledge",
                str(request.assignment_id),
                str(document_id),
                str(uuid4()),
            ],
        )
    )
    # upload_path owns its blocking file handle; cancellation may leave an orphan, not an index.
    try:
        with tempfile.TemporaryDirectory(prefix="huipi-knowledge-") as directory:
            path = Path(directory) / "source.txt"
            path.write_bytes(raw)
            await storage.upload_path(path, key, "text/plain; charset=utf-8")
    except StorageUnavailableError:
        raise KnowledgeError("knowledge_storage_unavailable") from None
    cleanup = False
    try:
        async with factory() as session:
            try:
                await require_scope(session, request.assignment_id, document_id, lock=True)
                previous = await session.scalar(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.document_id == document_id,
                        KnowledgeDocument.input_sha256 == input_sha,
                    )
                )
                if previous:
                    output = receipt(previous, reused=True)
                    await session.rollback()
                    cleanup = True  # No COMMIT was sent; current key is a confirmed loser.
                else:
                    active = list(
                        (
                            await session.scalars(
                                select(KnowledgeDocument).where(
                                    KnowledgeDocument.assignment_id == request.assignment_id,
                                    KnowledgeDocument.status == "active",
                                )
                            )
                        ).all()
                    )
                    old = next((r for r in active if r.document_id == document_id), None)
                    count = sum(r.chunk_count for r in active) - (old.chunk_count if old else 0)
                    if len(active) - bool(old) >= MAX_ACTIVE_DOCUMENTS or (
                        count + len(chunks) > MAX_SCOPE_CHUNKS
                    ):
                        raise KnowledgeError("knowledge_scope_capacity_exceeded")
                    version = 1 + (
                        await session.scalar(
                            select(func.max(KnowledgeDocument.source_version)).where(
                                KnowledgeDocument.document_id == document_id
                            )
                        )
                        or 0
                    )
                    if old:
                        old.status = "superseded"
                        await session.flush()  # Release partial unique active constraint first.
                    version_id = uuid5(
                        NAMESPACE_URL, f"huipi:material-version:{document_id}:{version}:{input_sha}"
                    )
                    row = KnowledgeDocument(
                        id=version_id,
                        document_id=document_id,
                        assignment_id=request.assignment_id,
                        source_version=version,
                        source_sha256=request.source_sha256,
                        input_sha256=input_sha,
                        title=request.title,
                        source_type=request.source_type,
                        permission=request.permission,
                        chunker_version=CHUNKER_VERSION,
                        status="active",
                        bucket=storage.bucket,
                        object_key=key,
                        size_bytes=len(raw),
                        chunk_count=len(chunks),
                    )
                    session.add(row)
                    await session.flush()
                    session.add_all(
                        [
                            KnowledgeChunk(
                                id=uuid5(version_id, f"{c.index}:{c.start}:{c.end}:{c.sha256}"),
                                document_version_id=version_id,
                                chunk_index=c.index,
                                chunk_text=c.text,
                                chunk_text_sha256=c.sha256,
                                text_start=c.start,
                                text_end=c.end,
                                content_pointer="/text",
                            )
                            for c in chunks
                        ]
                    )
                    await session.flush()
                    output = receipt(row, reused=False)
            except (KnowledgeError, SQLAlchemyError, OSError) as error:
                await session.rollback()
                cleanup = True
                if isinstance(error, KnowledgeError):
                    raise
                raise KnowledgeError("knowledge_import_rolled_back") from None
            if not cleanup:
                try:
                    await session.commit()
                except (SQLAlchemyError, OSError):
                    # A missing ACK or an empty follow-up query cannot prove non-commit.
                    raise KnowledgeError("knowledge_commit_outcome_unknown") from None
    except (SQLAlchemyError, OSError):
        raise KnowledgeError("knowledge_database_unavailable") from None
    finally:
        if cleanup:
            await _delete_loser(storage, key)
    return output


async def revoke_material(factory, assignment_id: UUID, document_id: UUID):
    if factory is None:
        raise KnowledgeError("knowledge_database_unavailable")
    try:
        async with factory() as session, session.begin():
            await require_scope(session, assignment_id, document_id, lock=True)
            rows = list(
                (
                    await session.scalars(
                        select(KnowledgeDocument).where(
                            KnowledgeDocument.document_id == document_id,
                            KnowledgeDocument.assignment_id == assignment_id,
                        )
                    )
                ).all()
            )
            if not rows:
                raise KnowledgeError("knowledge_not_found_in_scope")
            for row in rows:
                if row.status != "revoked":
                    row.status, row.revoked_at = "revoked", utc_now()
    except (SQLAlchemyError, OSError):
        raise KnowledgeError("knowledge_database_unavailable") from None


async def material_history(factory, assignment_id: UUID, document_id: UUID):
    if factory is None:
        raise KnowledgeError("knowledge_database_unavailable")
    try:
        async with factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(KnowledgeDocument)
                        .where(
                            KnowledgeDocument.assignment_id == assignment_id,
                            KnowledgeDocument.document_id == document_id,
                        )
                        .order_by(KnowledgeDocument.source_version.desc())
                        .limit(101)
                    )
                ).all()
            )
            if not rows:
                raise KnowledgeError("knowledge_not_found_in_scope")
            return {
                "versions": [receipt(r, reused=True).model_dump(mode="json") for r in rows[:100]],
                "truncated": len(rows) > 100,
            }
    except (SQLAlchemyError, OSError):
        raise KnowledgeError("knowledge_database_unavailable") from None
