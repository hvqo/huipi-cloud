"""Explicit local retrieval selection; lexical remains the stable default."""

from huipi_cloud.infrastructure.embedding.local import LocalEmbeddingProvider
from huipi_cloud.modules.knowledge.hybrid import DenseRetriever, HybridRetriever
from huipi_cloud.modules.knowledge.retriever import LexicalRetriever


def configured_retriever(factory, storage, config):
    lexical = LexicalRetriever(
        factory, storage, max_result_bytes=config.copilot_max_tool_result_bytes
    )
    if config.retrieval_mode == "lexical":
        return lexical
    embedding = LocalEmbeddingProvider(
        config.embedding_python,
        config.embedding_model_path,
        device=config.embedding_device,
        timeout=config.embedding_timeout_seconds,
    )
    dense = DenseRetriever(
        factory,
        storage,
        embedding,
        min_cosine=config.embedding_min_cosine,
        max_result_bytes=config.copilot_max_tool_result_bytes,
    )
    return (
        dense
        if config.retrieval_mode == "dense"
        else HybridRetriever(
            lexical, dense, allow_lexical_fallback=config.retrieval_allow_lexical_fallback
        )
    )
