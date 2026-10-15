"""Explicit local retrieval selection; lexical remains the stable default."""

import asyncio

from huipi_cloud.infrastructure.embedding.local import LocalEmbeddingProvider
from huipi_cloud.infrastructure.reranking.local import LocalRerankProvider
from huipi_cloud.modules.knowledge.hybrid import DenseRetriever, HybridRetriever
from huipi_cloud.modules.knowledge.rerank import RerankRetriever
from huipi_cloud.modules.knowledge.retriever import LexicalRetriever


def configured_retriever(factory, storage, config):
    lexical = LexicalRetriever(
        factory, storage, max_result_bytes=config.copilot_max_tool_result_bytes
    )
    if config.retrieval_mode == "lexical":
        return lexical
    gate = asyncio.Semaphore(1)
    embedding = LocalEmbeddingProvider(
        config.embedding_python,
        config.embedding_model_path,
        device=config.embedding_device,
        timeout=config.embedding_timeout_seconds,
        inference_gate=gate,
    )
    dense = DenseRetriever(
        factory,
        storage,
        embedding,
        min_cosine=config.embedding_min_cosine,
        max_result_bytes=config.copilot_max_tool_result_bytes,
    )
    source = (
        dense
        if config.retrieval_mode.startswith("dense")
        else HybridRetriever(
            lexical, dense, allow_lexical_fallback=config.retrieval_allow_lexical_fallback
        )
    )
    if not config.retrieval_mode.endswith("_rerank"):
        return source
    reranker = LocalRerankProvider(
        config.rerank_python,
        config.rerank_model_path,
        device=config.rerank_device,
        timeout=config.rerank_timeout_seconds,
        inference_gate=gate,
    )
    return RerankRetriever(
        source,
        reranker,
        requested_mode=config.retrieval_mode,
        candidate_limit=config.rerank_candidate_limit,
        min_score=config.rerank_min_score,
        allow_fallback=config.rerank_allow_fallback,
        max_result_bytes=config.copilot_max_tool_result_bytes,
    )
