"""Real PostgreSQL/S3; deterministic fake local models."""

from uuid import uuid4

import pytest

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.reranking.protocol import RerankError
from huipi_cloud.modules.copilot.protocol import CopilotRequest
from huipi_cloud.modules.copilot.runtime import CopilotRuntime
from huipi_cloud.modules.copilot.tools import BusinessTools
from huipi_cloud.modules.knowledge.rerank import RerankRetriever
from huipi_cloud.modules.knowledge.schemas import KnowledgeError
from huipi_cloud.modules.knowledge.service import revoke_material
from tests.integration.test_hybrid_retrieval import retrievers
from tests.integration.test_scoped_retrieval import RagProvider, assignment, put
from tests.unit.test_reranking import FakeReranker


def wrapper(factory, storage, *, mode="hybrid_rerank", fallback=False, budget=8192):
    embedding, lexical, dense, hybrid = retrievers(factory, storage)
    reranker = FakeReranker()
    source = dense if mode == "dense_rerank" else hybrid
    return (
        embedding,
        reranker,
        RerankRetriever(
            source, reranker, requested_mode=mode, allow_fallback=fallback, max_result_bytes=budget
        ),
    )


@pytest.mark.anyio
async def test_internal_candidates_exceed_five_but_public_limit_stays_five(
    client,
    postgres_engine,
    recording_minio_storage,
):
    factory, aid = await assignment(client, postgres_engine)
    for index in range(24):
        await put(
            factory,
            recording_minio_storage,
            aid,
            f"热传导接触能量，合成资料{index}。",
            title=f"资料{index}",
        )
    _, lexical, dense, hybrid = retrievers(factory, recording_minio_storage)
    for source in [lexical, dense, hybrid]:
        candidates = await source.retrieve_candidates(aid, "热传导", 20)
        assert len(candidates.hits) == 20 and candidates.truncated
        assert len({h.chunk_id for h in candidates.hits}) == 20
        assert len((await source.search(aid, "热传导", 5)).hits) <= 5
        with pytest.raises(KnowledgeError):
            await source.retrieve_candidates(aid, "热传导", 21)
    scorer = FakeReranker()
    candidates = await dense.retrieve_candidates(aid, "热传导", 20)
    scorer.scores = {h.chunk_id: (1 if i == 15 else 0.1) for i, h in enumerate(candidates.hits)}
    result = await RerankRetriever(dense, scorer, requested_mode="dense_rerank").search(
        aid, "热传导", 5
    )
    assert result.candidate_count == 20 and len(result.hits) == 1
    assert result.hits[0].chunk_id == candidates.hits[15].chunk_id
    assert len(scorer.calls[0]) == 20


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["dense_rerank", "hybrid_rerank"])
async def test_rerank_reorders_with_stable_citations_and_rejects_foreign_or_revoked(
    client,
    postgres_engine,
    recording_minio_storage,
    mode,
):
    factory, aid = await assignment(client, postgres_engine)
    _, foreign = await assignment(client, postgres_engine)
    own = await put(factory, recording_minio_storage, aid, "热传导接触传热。")
    await put(factory, recording_minio_storage, foreign, "热传导其他作业私密。")
    _, scorer, search = wrapper(factory, recording_minio_storage, mode=mode)
    result = await search.search(aid, "热传导", 3)
    assert result.hits[0].document_id == own.document_id
    assert result.retrieval_mode == mode and not result.rerank_degraded
    assert result.rerank_input_tokens == 50
    with pytest.raises(KnowledgeError, match="citation_source_stale"):
        await search.validate_citations(foreign, result.hits)
    with pytest.raises(KnowledgeError):
        await search.validate_citations(
            aid, [result.hits[0].model_copy(update={"chunk_id": uuid4()})]
        )
    await revoke_material(factory, aid, own.document_id)
    assert not (await search.search(aid, "热传导", 3)).hits


@pytest.mark.anyio
async def test_revoked_during_rerank_never_becomes_valid_answer(
    client,
    postgres_engine,
    recording_minio_storage,
):
    factory, aid = await assignment(client, postgres_engine)
    doc = await put(factory, recording_minio_storage, aid)
    _, scorer, search = wrapper(factory, recording_minio_storage, fallback=True)

    async def revoke():
        await revoke_material(factory, aid, doc.document_id)

    scorer.after_score = revoke
    with pytest.raises(KnowledgeError, match="citation_source_stale"):
        await search.search(aid, "热传导", 1)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    ["rerank_model_unavailable", "rerank_out_of_memory", "rerank_timeout", "rerank_token_limit"],
)
async def test_explicit_fallback_preserves_both_failure_reasons_and_actual_mode(
    client,
    postgres_engine,
    recording_minio_storage,
    failure,
):
    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    embedding, scorer, search = wrapper(factory, recording_minio_storage, fallback=True)
    embedding.fail = "embedding_model_unavailable"
    scorer.error = failure
    result = await search.search(aid, "热传导", 1)
    assert result.retrieval_mode == "lexical" and result.requested_mode == "hybrid_rerank"
    assert result.degraded and result.degradation_reason == "embedding_model_unavailable"
    assert result.rerank_degraded and result.rerank_degradation_reason == failure
    search.allow_fallback = False
    with pytest.raises(RerankError, match=failure):
        await search.search(aid, "热传导", 1)


@pytest.mark.anyio
async def test_bad_source_never_reaches_reranker_or_degrades(
    client,
    postgres_engine,
    recording_minio_storage,
):
    from io import BytesIO

    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    key = next(iter(recording_minio_storage.uploaded_keys))
    await recording_minio_storage.upload_fileobj(BytesIO(b"corrupt"), key, "text/plain")
    embedding, scorer, search = wrapper(factory, recording_minio_storage, fallback=True)
    with pytest.raises(KnowledgeError, match="sha_mismatch"):
        await search.search(aid, "热传导", 1)
    assert not scorer.calls and not embedding.documents


@pytest.mark.anyio
async def test_budget_and_agent_trace_are_explicit_without_new_tool_permissions(
    client,
    postgres_engine,
    recording_minio_storage,
):
    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    _, scorer, search = wrapper(factory, recording_minio_storage, fallback=True)
    scorer.error = "rerank_timeout"
    result = await CopilotRuntime(
        RagProvider(), BusinessTools(factory, aid, retriever=search), Settings()
    ).run(CopilotRequest(assignment_id=aid, question="查询热传导", require_material_evidence=True))
    assert result.status == "completed" and result.citations
    event = next(t for t in result.trace if t.kind == "tool")
    assert event.rerank_degraded and event.rerank_degradation_reason == "rerank_timeout"
    assert event.requested_mode == "hybrid_rerank" and event.retrieval_mode == "hybrid"
    search.max_result_bytes = 200
    with pytest.raises(KnowledgeError, match="budget_too_small"):
        await search.search(aid, "热传导", 1)


@pytest.mark.anyio
async def test_version_replacement_never_reuses_old_reranked_citation(
    client,
    postgres_engine,
    recording_minio_storage,
):
    factory, aid = await assignment(client, postgres_engine)
    original = await put(factory, recording_minio_storage, aid)
    _, scorer, search = wrapper(factory, recording_minio_storage)
    first = await search.search(aid, "热传导", 1)
    await put(
        factory,
        recording_minio_storage,
        aid,
        "热传导新版本接触传热。",
        document_id=original.document_id,
    )
    second = await search.search(aid, "热传导", 1)
    assert second.hits[0].source_version == 2
    assert first.hits[0].chunk_id != second.hits[0].chunk_id
    with pytest.raises(KnowledgeError, match="citation_source_stale"):
        await search.validate_citations(aid, first.hits)
    assert scorer.calls[-1] == [second.hits[0].chunk_id]


@pytest.mark.anyio
async def test_database_and_storage_failures_cannot_be_converted_to_rerank_fallback():
    from huipi_cloud.modules.knowledge.retriever import LexicalRetriever

    source = LexicalRetriever(None, None)
    scorer = FakeReranker()
    retriever = RerankRetriever(source, scorer, requested_mode="hybrid_rerank", allow_fallback=True)
    with pytest.raises(KnowledgeError, match="database_unavailable"):
        await retriever.search(uuid4(), "材料", 1)
    assert not scorer.calls
