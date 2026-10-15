"""Bounded Chinese lexical baseline, not BM25; source-verified, citation-ready output."""

import re
import unicodedata
from contextlib import aclosing
from types import SimpleNamespace
from typing import Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.knowledge.chunking import decode_source, sha256
from huipi_cloud.modules.knowledge.models import KnowledgeChunk, KnowledgeDocument
from huipi_cloud.modules.knowledge.schemas import (
    MAX_SCOPE_CHUNKS,
    MAX_SOURCE_BYTES,
    KnowledgeError,
    RetrievalHit,
    SearchResult,
)


class Retriever(Protocol):
    async def search(self, assignment_id: UUID, query: str, top_k: int) -> SearchResult: ...
    async def validate_citations(self, assignment_id: UUID, hits: list[RetrievalHit]) -> None: ...


def terms(value: str) -> dict[str, int]:
    value = unicodedata.normalize("NFKC", value).casefold()
    result = {s: 3 for s in re.findall(r"[a-z0-9]+", value)}
    for sequence in re.findall(r"[\u3400-\u9fff]+", value):
        result.update({c: 1 for c in sequence})
        result.update({sequence[i : i + 2]: 3 for i in range(len(sequence) - 1)})
    return result


def lexical_score(query: str, content: str) -> float:
    q, doc = terms(query), terms(content)
    return (
        round(sum(weight for t, weight in q.items() if t in doc) / sum(q.values()), 8) if q else 0
    )


class LexicalRetriever:
    def __init__(self, factory, storage, *, max_result_bytes=8192):
        self.factory, self.storage = factory, storage
        self.max_result_bytes = max_result_bytes

    async def _rows(self, assignment_id, chunk_ids=None):
        if self.factory is None:
            raise KnowledgeError("knowledge_database_unavailable")
        try:
            async with self.factory() as session, session.begin():
                await session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                await session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
                query = (
                    select(KnowledgeDocument, KnowledgeChunk)
                    .join(
                        KnowledgeChunk, KnowledgeChunk.document_version_id == KnowledgeDocument.id
                    )
                    .where(
                        KnowledgeDocument.assignment_id == assignment_id,
                        KnowledgeDocument.status == "active",
                        KnowledgeDocument.permission.in_(["synthetic", "authorized"]),
                    )
                )
                if chunk_ids is not None:
                    query = query.where(KnowledgeChunk.id.in_(chunk_ids))
                rows = (
                    await session.execute(
                        query.order_by(
                            KnowledgeDocument.document_id,
                            KnowledgeDocument.source_version,
                            KnowledgeChunk.chunk_index,
                        ).limit(MAX_SCOPE_CHUNKS + 1)
                    )
                ).all()
                if len(rows) > MAX_SCOPE_CHUNKS:
                    raise KnowledgeError("knowledge_scope_capacity_exceeded")

                # Materialize snapshots before read-only COMMIT, including expire_on_commit=True.
                def snapshot(row):
                    return SimpleNamespace(
                        **{col.name: getattr(row, col.name) for col in row.__table__.columns}
                    )

                return [(snapshot(d), snapshot(c)) for d, c in rows]
        except (SQLAlchemyError, OSError):
            raise KnowledgeError("knowledge_database_unavailable") from None

    async def _verify_source(self, doc, chunk, cache):
        if self.storage is None or self.storage.bucket != doc.bucket:
            raise KnowledgeError("knowledge_storage_location_mismatch")
        if doc.id not in cache:
            try:
                downloaded = await self.storage.download(doc.object_key)
                payload = bytearray()
                async with aclosing(downloaded.chunks()) as stream:
                    async for part in stream:
                        payload.extend(part)
                        if len(payload) > MAX_SOURCE_BYTES:
                            raise KnowledgeError("knowledge_source_size_limit")
                if len(payload) != doc.size_bytes:
                    raise KnowledgeError("knowledge_source_sha_mismatch")
                cache[doc.id] = decode_source(bytes(payload), doc.source_sha256)
            except StorageUnavailableError:
                raise KnowledgeError("knowledge_source_unavailable") from None
        if (
            cache[doc.id][chunk.text_start : chunk.text_end] != chunk.chunk_text
            or sha256(chunk.chunk_text.encode()) != chunk.chunk_text_sha256
        ):
            raise KnowledgeError("knowledge_chunk_source_mismatch")

    @staticmethod
    def _hit(doc, chunk, score):
        return RetrievalHit(
            citation_id="K" + chunk.id.hex,
            document_id=doc.document_id,
            title=doc.title,
            source_version=doc.source_version,
            source_sha256=doc.source_sha256,
            chunk_id=chunk.id,
            chunk_text_sha256=chunk.chunk_text_sha256,
            text_start=chunk.text_start,
            text_end=chunk.text_end,
            content_pointer=chunk.content_pointer,
            excerpt=chunk.chunk_text,
            relevance_score=score,
            truncated=False,
        )

    async def search(self, assignment_id: UUID, query: str, top_k: int) -> SearchResult:
        if (
            not isinstance(query, str)
            or not 1 <= len(query.strip()) <= 250
            or (type(top_k) is not int or not 1 <= top_k <= 5)
        ):
            raise KnowledgeError("knowledge_invalid_search")
        rows = await self._rows(assignment_id)
        ranked = sorted(
            ((lexical_score(query, c.chunk_text), d, c) for d, c in rows),
            key=lambda r: (-r[0], str(r[1].document_id), r[2].chunk_index),
        )
        ranked = [r for r in ranked if r[0] > 0]
        result, cache = SearchResult(), {}
        for score, doc, chunk in ranked[:top_k]:
            await self._verify_source(doc, chunk, cache)
            hit = self._hit(doc, chunk, score)
            candidate = SearchResult(hits=[*result.hits, hit], truncated=True)
            # Reserve space for the outer Agent tool status/data wrapper.
            if len(candidate.model_dump_json().encode()) + 128 > self.max_result_bytes:
                if not result.hits:
                    raise KnowledgeError("knowledge_result_budget_too_small")
                break
            result = candidate
        output = SearchResult(hits=result.hits, truncated=len(ranked) > len(result.hits))
        await self.validate_citations(assignment_id, output.hits)
        return output

    async def validate_citations(self, assignment_id: UUID, hits: list[RetrievalHit]) -> None:
        if not hits:
            return
        rows = await self._rows(assignment_id, [h.chunk_id for h in hits])
        by_id, cache = {c.id: (d, c) for d, c in rows}, {}
        for hit in hits:
            pair = by_id.get(hit.chunk_id)
            if pair is None:
                raise KnowledgeError("citation_source_stale")
            doc, chunk = pair
            # Identity, version, range, and exact original excerpt must all agree.
            expected = self._hit(doc, chunk, hit.relevance_score)
            if expected != hit:
                raise KnowledgeError("citation_source_mismatch")
            await self._verify_source(doc, chunk, cache)
        # Recheck after object I/O; revocation/version replacement during the read must fail.
        current = {
            c.id: (d, c) for d, c in await self._rows(assignment_id, [h.chunk_id for h in hits])
        }
        for hit in hits:
            pair = current.get(hit.chunk_id)
            if pair is None:
                raise KnowledgeError("citation_source_stale")
            if self._hit(*pair, hit.relevance_score) != hit:
                raise KnowledgeError("citation_source_mismatch")
