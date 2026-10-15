"""Scoped dense exact search and deterministic RRF, with the original citation verifier."""

import asyncio
import math
import time

from huipi_cloud.infrastructure.embedding.protocol import EmbeddingError, normalize
from huipi_cloud.modules.knowledge.retriever import LexicalRetriever
from huipi_cloud.modules.knowledge.schemas import KnowledgeError, SearchResult
from huipi_cloud.modules.knowledge.vector_index import MemoryVectorIndex, VectorKey

DENSE_VERSION = "scoped-cosine-v1"
HYBRID_VERSION = "scoped-rrf-v1"


def rrf(rankings, *, k=60):
    """sum(1/(k+rank)), rank starts at 1; each list contributes once per chunk."""
    if type(k) is not int or k <= 0:
        raise ValueError("rrf_invalid_k")
    scores = {}
    for ranking in rankings:
        seen = set()
        for rank, chunk_id in enumerate(ranking, 1):
            if chunk_id not in seen:
                scores[chunk_id] = scores.get(chunk_id, 0) + 1 / (k + rank)
                seen.add(chunk_id)
    return sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))


async def bounded_output(verifier, aid, hits, top_k, *, upstream_truncated=False, **metadata):
    result = SearchResult(**metadata)
    for hit in hits[:top_k]:
        candidate = SearchResult(hits=[*result.hits, hit], truncated=False, **metadata)
        if len(candidate.model_dump_json().encode()) + 128 > verifier.max_result_bytes:
            if not result.hits:
                raise KnowledgeError("knowledge_result_budget_too_small")
            break
        result = candidate
    result = result.model_copy(
        update={"truncated": upstream_truncated or len(hits) > len(result.hits)}
    )
    await verifier.validate_citations(aid, result.hits)
    return result


def validate_search(query, top_k):
    if (
        not isinstance(query, str)
        or not 1 <= len(query.strip()) <= 250
        or type(top_k) is not int
        or not 1 <= top_k <= 5
    ):
        raise KnowledgeError("knowledge_invalid_search")


class DenseRetriever:
    def __init__(
        self,
        factory,
        storage,
        embedding,
        *,
        index=None,
        min_cosine=0.55,
        max_result_bytes=8192,
        auto_index=True,
    ):
        if not math.isfinite(min_cosine) or not -1 <= min_cosine <= 1:
            raise ValueError("dense_invalid_threshold")
        self.verifier = LexicalRetriever(factory, storage, max_result_bytes=max_result_bytes)
        self.embedding = embedding
        self.index = index or MemoryVectorIndex()
        self.min_cosine, self.auto_index = min_cosine, auto_index
        self.lock = asyncio.Lock()
        self.last_index_seconds = 0.0
        self.index_status = "not_built"

    async def prepare(self, aid):
        """Explicit warmup/rebuild entry; query also rebuilds missing current keys if enabled."""
        rows = await self.verifier._rows(aid)
        return await self._ensure(aid, rows, force=True)

    async def _ensure(self, aid, rows, *, force=False):
        identity = self.embedding.identity
        keys = [VectorKey.for_chunk(aid, d, c, identity) for d, c in rows]
        async with self.lock:
            missing = [
                (key, d, c) for key, (d, c) in zip(keys, rows) if self.index.get(key) is None
            ]
            if missing and not (self.auto_index or force):
                self.index_status = "not_ready"
                raise EmbeddingError("embedding_index_not_ready")
            started, cache, pending = time.perf_counter(), {}, {}
            try:
                # Verify before sending any raw source text to the local feature extractor.
                for _, doc, chunk in missing:
                    await self.verifier._verify_source(doc, chunk, cache)
                for offset in range(0, len(missing), identity.batch_size):
                    batch = missing[offset : offset + identity.batch_size]
                    vectors = await self.embedding.embed_documents(
                        [c.chunk_text for _, _, c in batch]
                    )
                    if not isinstance(vectors, list) or len(vectors) != len(batch):
                        raise EmbeddingError("embedding_invalid_output")
                    pending.update(
                        {
                            key: normalize(vector, identity.dimension)
                            for (key, _, _), vector in zip(batch, vectors)
                        }
                    )
                # DB state may have changed while the model was running. Never publish stale keys.
                current = await self.verifier._rows(aid)
                if keys != [VectorKey.for_chunk(aid, d, c, identity) for d, c in current]:
                    raise KnowledgeError("embedding_source_changed_during_index")
                if self.embedding.identity != identity:
                    raise EmbeddingError("embedding_config_changed")
                self.index.publish(pending)
                self.index_status = "ready"
                if missing:
                    self.last_index_seconds = time.perf_counter() - started
            except BaseException:
                self.index_status = "not_ready"
                raise
            # Pin an immutable local snapshot; later cache eviction cannot truncate this search.
            vectors = [self.index.get(key) for key in keys]
            if any(v is None for v in vectors):
                raise EmbeddingError("embedding_index_not_ready")
            return vectors, bool(missing)

    async def search(self, assignment_id, query, top_k):
        validate_search(query, top_k)
        identity = self.embedding.identity
        rows = await self.verifier._rows(assignment_id)
        vectors, rebuilt = await self._ensure(assignment_id, rows)
        q = normalize(await self.embedding.embed_query(query), identity.dimension)
        if self.embedding.identity != identity:
            raise EmbeddingError("embedding_config_changed")
        ranked = []
        for (doc, chunk), vector in zip(rows, vectors):
            cosine = max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(q, vector))))
            if cosine >= self.min_cosine:
                ranked.append((cosine, doc, chunk))
        ranked.sort(key=lambda r: (-r[0], str(r[1].document_id), r[2].chunk_index))
        hits = [self.verifier._hit(d, c, (score + 1) / 2) for score, d, c in ranked]
        return await bounded_output(
            self.verifier,
            assignment_id,
            hits,
            top_k,
            retrieval_mode="dense",
            retriever_version=DENSE_VERSION,
            embedding_model_version=self.embedding.identity.digest,
            index_status="rebuilt" if rebuilt else "ready",
            score_meaning="(cosine+1)/2_ranking_signal_not_probability_or_teaching_score",
        )

    async def aclose(self):
        await self.embedding.aclose()

    async def validate_citations(self, aid, hits):
        await self.verifier.validate_citations(aid, hits)


class HybridRetriever:
    def __init__(self, lexical, dense, *, allow_lexical_fallback=True):
        self.lexical, self.dense = lexical, dense
        self.allow_lexical_fallback = allow_lexical_fallback
        self.preparation_failure = None

    async def prepare(self, aid):
        self.preparation_failure = None
        try:
            await self.dense.prepare(aid)
        except EmbeddingError as error:
            self.preparation_failure = error.code
            if not self.allow_lexical_fallback:
                raise
            # Preserve the failure until explicit prepare() recovery, without another launch.

    async def search(self, assignment_id, query, top_k):
        validate_search(query, top_k)
        lexical = await self.lexical.search(assignment_id, query, 5)
        try:
            if self.preparation_failure:
                raise EmbeddingError(self.preparation_failure)
            dense = await self.dense.search(assignment_id, query, 5)
        except EmbeddingError as error:
            if not self.allow_lexical_fallback:
                raise
            return await bounded_output(
                self.lexical,
                assignment_id,
                lexical.hits,
                top_k,
                retrieval_mode="lexical",
                requested_mode="hybrid",
                degraded=True,
                degradation_reason=error.code,
                upstream_truncated=lexical.truncated,
                retriever_version=HYBRID_VERSION,
                score_meaning=lexical.score_meaning,
                index_status="not_ready",
            )
        # Detect source version changes across the two independent retrieval branches too.
        by_id = {h.chunk_id: h for h in lexical.hits}
        for hit in dense.hits:
            if hit.chunk_id in by_id:
                previous = by_id[hit.chunk_id].model_copy(
                    update={"relevance_score": hit.relevance_score}
                )
                if previous != hit:
                    raise KnowledgeError("citation_source_mismatch")
            by_id[hit.chunk_id] = hit
        combined = rrf([[h.chunk_id for h in lexical.hits], [h.chunk_id for h in dense.hits]])
        hits = [by_id[cid].model_copy(update={"relevance_score": score}) for cid, score in combined]
        return await bounded_output(
            self.lexical,
            assignment_id,
            hits,
            top_k,
            retrieval_mode="hybrid",
            requested_mode="hybrid",
            retriever_version=HYBRID_VERSION,
            embedding_model_version=self.dense.embedding.identity.digest,
            index_status=dense.index_status,
            upstream_truncated=lexical.truncated or dense.truncated,
            score_meaning="RRF_sum_1/(60+rank)_not_probability_or_teaching_score",
        )

    async def aclose(self):
        await self.dense.aclose()

    async def validate_citations(self, aid, hits):
        await self.lexical.validate_citations(aid, hits)
