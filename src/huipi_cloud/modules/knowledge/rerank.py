"""Two-stage retrieval, preserving every original citation identity and source check."""

import math

from huipi_cloud.infrastructure.reranking.protocol import (
    RerankError,
    validate_candidates,
    validate_reply,
)
from huipi_cloud.modules.knowledge.hybrid import bounded_output, validate_search
from huipi_cloud.modules.knowledge.schemas import KnowledgeError

RERANK_VERSION = "scoped-rerank-v1"


class RerankRetriever:
    def __init__(
        self,
        source,
        reranker,
        *,
        requested_mode,
        candidate_limit=20,
        min_score=0.5,
        allow_fallback=False,
        max_result_bytes=8192,
    ):
        if (
            requested_mode not in {"dense_rerank", "hybrid_rerank"}
            or type(candidate_limit) is not int
            or not 1 <= candidate_limit <= 20
            or not math.isfinite(min_score)
            or not 0 <= min_score <= 1
        ):
            raise ValueError("rerank_invalid_retrieval_config")
        self.source, self.reranker = source, reranker
        self.requested_mode, self.candidate_limit = requested_mode, candidate_limit
        self.min_score, self.allow_fallback = min_score, allow_fallback
        self.max_result_bytes = max_result_bytes
        self.preparation_failure = None

    async def prepare(self, aid):
        if hasattr(self.source, "prepare"):
            await self.source.prepare(aid)
        self.preparation_failure = None
        try:
            await self.reranker.warmup()
        except RerankError as error:
            self.preparation_failure = error.code
            if not self.allow_fallback:
                raise

    async def validate_citations(self, aid, hits):
        await self.source.validate_citations(aid, hits)

    async def rank_candidates(self, aid, query, candidates):
        checked = validate_candidates(query, candidates.hits)
        await self.validate_citations(aid, checked)  # before text reaches the local classifier
        metadata = candidates.model_dump(exclude={"hits", "truncated"})
        metadata.update(
            requested_mode=self.requested_mode,
            candidate_count=len(checked),
            retriever_version=RERANK_VERSION,
        )
        try:
            if self.preparation_failure:
                raise RerankError(self.preparation_failure)
            reply = await self.reranker.rerank(query, checked)
            validate_reply(reply, checked, self.reranker.identity)
        except RerankError as error:
            if not self.allow_fallback:
                raise
            metadata.update(
                actual_mode=candidates.retrieval_mode,
                degraded=True,
                degradation_reason=metadata.get("degradation_reason") or error.code,
                rerank_degraded=True,
                rerank_degradation_reason=error.code,
            )
            # Keep the Embedding failure reason too; neither stage impersonates success.
            await self.validate_citations(aid, checked)
            return checked, metadata
        hits = sorted(checked, key=lambda h: (-reply.scores[h.chunk_id], str(h.chunk_id)))
        hits = [
            h.model_copy(update={"relevance_score": reply.scores[h.chunk_id]})
            for h in hits
            if reply.scores[h.chunk_id] >= self.min_score
        ]
        await self.validate_citations(aid, hits)
        # If Embedding fell back, reranking still happened, but actual base is lexical.
        actual = "lexical_rerank" if candidates.degraded else self.requested_mode
        metadata.update(
            retrieval_mode=actual,
            actual_mode=actual,
            reranker_model_version=self.reranker.identity.digest,
            rerank_elapsed_ms=reply.elapsed_ms,
            rerank_input_tokens=reply.input_tokens,
            score_meaning="yes-no_relevance_score_not_fact_probability_or_grade",
        )
        return hits, metadata

    async def search(self, assignment_id, query, top_k):
        validate_search(query, top_k)
        candidates = await self.source.retrieve_candidates(
            assignment_id, query, self.candidate_limit
        )
        if len(candidates.hits) > self.candidate_limit:
            raise KnowledgeError("knowledge_candidate_limit_exceeded")
        hits, metadata = await self.rank_candidates(assignment_id, query, candidates)
        return await bounded_output(
            self, assignment_id, hits, top_k, upstream_truncated=candidates.truncated, **metadata
        )

    async def aclose(self):
        try:
            await self.reranker.aclose()
        finally:
            if hasattr(self.source, "aclose"):
                await self.source.aclose()
