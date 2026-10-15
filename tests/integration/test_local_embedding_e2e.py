"""Real local feature extraction, explicitly opted in; no downloads, no paid calls."""

import os

import pytest

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.embedding.local import LocalEmbeddingProvider
from huipi_cloud.modules.knowledge.hybrid import DenseRetriever, HybridRetriever
from huipi_cloud.modules.knowledge.retriever import LexicalRetriever
from huipi_cloud.modules.knowledge.service import revoke_material
from tests.integration.test_scoped_retrieval import assignment, put


@pytest.mark.anyio
@pytest.mark.embedding_e2e
async def test_real_local_model_synonym_scoped_source_and_revocation(
    client,
    postgres_engine,
    recording_minio_storage,
):
    if os.getenv("RUN_LOCAL_EMBEDDING") != "1":
        pytest.skip("local Embedding is explicit opt-in, never download in CI")
    cfg = Settings()
    factory, aid = await assignment(client, postgres_engine)
    _, foreign = await assignment(client, postgres_engine)
    own = await put(factory, recording_minio_storage, aid, "热传导通过直接接触传递能量。")
    await put(factory, recording_minio_storage, foreign, "别的作业：热传导的合成答案是紫色木星。")
    embedding = LocalEmbeddingProvider(
        cfg.embedding_python, cfg.embedding_model_path, device=cfg.embedding_device
    )
    dense = DenseRetriever(factory, recording_minio_storage, embedding)
    hybrid = HybridRetriever(
        LexicalRetriever(factory, recording_minio_storage), dense, allow_lexical_fallback=False
    )
    try:
        result = await hybrid.search(aid, "不同温度的金属碰在一起怎样传热？", 3)
        assert result.hits and result.hits[0].document_id == own.document_id
        assert result.retrieval_mode == "hybrid" and not result.degraded
        assert embedding.process and embedding.process.returncode is None
        assert len(next(iter(dense.index.vectors.values()))) == 1024
        await hybrid.validate_citations(aid, result.hits)
        await revoke_material(factory, aid, own.document_id)
        assert not (await hybrid.search(aid, "ZXREVOKED6677", 3)).hits
    finally:
        process = embedding.process
        await embedding.aclose()
        assert process is None or process.returncode is not None
