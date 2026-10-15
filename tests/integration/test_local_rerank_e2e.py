"""Real local models, explicit opt-in; no default CI weights or paid API."""

import asyncio
import os

import pytest

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.embedding.local import LocalEmbeddingProvider
from huipi_cloud.infrastructure.reranking.local import LocalRerankProvider
from huipi_cloud.modules.knowledge.hybrid import DenseRetriever
from huipi_cloud.modules.knowledge.rerank import RerankRetriever
from huipi_cloud.modules.knowledge.service import revoke_material
from tests.integration.test_scoped_retrieval import assignment, put


@pytest.mark.anyio
@pytest.mark.rerank_e2e
async def test_real_embedding_and_rerank_share_gpu_and_keep_source_contract(
    client,
    postgres_engine,
    recording_minio_storage,
):
    if os.getenv("RUN_LOCAL_RERANK") != "1":
        pytest.skip("explicit local Rerank opt-in required")
    cfg = Settings()
    gate = asyncio.Semaphore(1)
    embedding = LocalEmbeddingProvider(
        cfg.embedding_python,
        cfg.embedding_model_path,
        device=cfg.embedding_device,
        inference_gate=gate,
    )
    reranker = LocalRerankProvider(
        cfg.rerank_python, cfg.rerank_model_path, device=cfg.rerank_device, inference_gate=gate
    )
    factory, aid = await assignment(client, postgres_engine)
    doc = await put(factory, recording_minio_storage, aid, "热传导通过直接接触传递能量。")
    source = DenseRetriever(factory, recording_minio_storage, embedding)
    retriever = RerankRetriever(source, reranker, requested_mode="dense_rerank")
    try:
        await retriever.prepare(aid)
        result = await retriever.search(aid, "物体直接贴合怎样传热？", 3)
        assert result.hits and result.hits[0].document_id == doc.document_id
        assert result.actual_mode == "dense_rerank" and not result.rerank_degraded
        assert result.rerank_input_tokens > 0
        assert embedding.process.returncode is None and reranker.process.returncode is None
        await revoke_material(factory, aid, doc.document_id)
        assert not (await retriever.search(aid, "接触传热", 3)).hits
    finally:
        processes = [embedding.process, reranker.process]
        await retriever.aclose()
        assert all(p is None or p.returncode is not None for p in processes)
