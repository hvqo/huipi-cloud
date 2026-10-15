"""Real scoped PG/S3 with fake feature vectors; not model-quality evidence."""

from dataclasses import replace
from uuid import uuid4

import pytest

from huipi_cloud.infrastructure.embedding.protocol import EmbeddingError
from huipi_cloud.modules.copilot.protocol import CopilotRequest
from huipi_cloud.modules.copilot.runtime import CopilotRuntime
from huipi_cloud.modules.copilot.tools import BusinessTools
from huipi_cloud.modules.knowledge.hybrid import DenseRetriever, HybridRetriever
from huipi_cloud.modules.knowledge.retriever import LexicalRetriever
from huipi_cloud.modules.knowledge.schemas import KnowledgeError
from huipi_cloud.modules.knowledge.service import revoke_material
from tests.integration.test_scoped_retrieval import RagProvider, assignment, put
from tests.unit.test_embedding_retrieval import FakeEmbedding


def retrievers(factory, storage, *, budget=8192, auto=True):
    fake = FakeEmbedding()
    lexical = LexicalRetriever(factory, storage, max_result_bytes=budget)
    dense = DenseRetriever(factory, storage, fake, max_result_bytes=budget, auto_index=auto)
    return fake, lexical, dense, HybridRetriever(lexical, dense)


@pytest.mark.anyio
async def test_dense_hybrid_versioned_rebuild_idempotence_and_assignment_filter(
    client,
    postgres_engine,
    recording_minio_storage,
):
    factory, aid = await assignment(client, postgres_engine)
    _, foreign = await assignment(client, postgres_engine)
    doc = await put(factory, recording_minio_storage, aid, "热传导通过接触传递能量。")
    await put(factory, recording_minio_storage, foreign, "热传导属于其他作业私密资料。")
    fake, lexical, dense, hybrid = retrievers(factory, recording_minio_storage)
    first = await dense.search(aid, "导热", 3)
    assert first.hits[0].document_id == doc.document_id and first.index_status == "rebuilt"
    assert fake.documents == ["热传导通过接触传递能量。"]
    await dense.search(aid, "导热", 3)
    assert len(fake.documents) == 1 and len(dense.index.vectors) == 1
    fused = await hybrid.search(aid, "导热", 3)
    assert fused.retrieval_mode == "hybrid" and not fused.degraded
    assert len(fused.hits) == 1 and fused.hits[0].citation_id == first.hits[0].citation_id
    fake.identity = replace(fake.identity, model_revision="test-v2")
    await dense.search(aid, "导热", 3)
    assert len(fake.documents) == 2  # model changes require new index identity
    await put(
        factory, recording_minio_storage, aid, "热传导新版本接触传热。", document_id=doc.document_id
    )
    changed = await dense.search(aid, "导热", 3)
    assert changed.hits[0].source_version == 2
    assert first.hits[0].chunk_id != changed.hits[0].chunk_id
    assert len(fake.documents) == 3
    with pytest.raises(KnowledgeError, match="citation_source_stale"):
        await hybrid.validate_citations(aid, first.hits)
    await revoke_material(factory, aid, doc.document_id)
    assert not (await dense.search(aid, "导热", 3)).hits
    assert not (await hybrid.search(aid, "导热", 3)).hits


@pytest.mark.anyio
async def test_missing_index_is_explicit_and_manual_rebuild_recovers(
    client,
    postgres_engine,
    recording_minio_storage,
):
    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    _, lexical, dense, hybrid = retrievers(factory, recording_minio_storage, auto=False)
    with pytest.raises(EmbeddingError, match="index_not_ready"):
        await dense.search(aid, "热传导", 1)
    fallback = await hybrid.search(aid, "热传导", 1)
    assert fallback.degraded and fallback.retrieval_mode == "lexical"
    assert fallback.degradation_reason == "embedding_index_not_ready"
    await dense.prepare(aid)
    assert (await hybrid.search(aid, "热传导", 1)).retrieval_mode == "hybrid"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code",
    [
        "embedding_timeout",
        "embedding_out_of_memory",
        "embedding_dimension_mismatch",
        "embedding_non_finite_vector",
    ],
)
async def test_embedding_failure_degrades_explicitly_without_index_or_material_writes(
    client,
    postgres_engine,
    recording_minio_storage,
    code,
):
    factory, aid = await assignment(client, postgres_engine)
    doc = await put(factory, recording_minio_storage, aid)
    fake, lexical, dense, hybrid = retrievers(factory, recording_minio_storage)
    fake.fail = code
    before = set(recording_minio_storage.uploaded_keys)
    result = await hybrid.search(aid, "热传导", 1)
    assert result.degraded and result.degradation_reason == code
    assert result.requested_mode == "hybrid" and result.retrieval_mode == "lexical"
    assert result.hits[0].document_id == doc.document_id
    assert dense.index_status == "not_ready" and len(dense.index.vectors) == 0
    assert before == recording_minio_storage.uploaded_keys
    strict = HybridRetriever(lexical, dense, allow_lexical_fallback=False)
    with pytest.raises(EmbeddingError, match=code):
        await strict.search(aid, "热传导", 1)


@pytest.mark.anyio
async def test_revocation_during_index_build_does_not_publish_stale_vectors(
    client,
    postgres_engine,
    recording_minio_storage,
):
    factory, aid = await assignment(client, postgres_engine)
    doc = await put(factory, recording_minio_storage, aid)
    fake, _, dense, _ = retrievers(factory, recording_minio_storage)

    async def revoke():
        await revoke_material(factory, aid, doc.document_id)

    fake.after_embed = revoke
    with pytest.raises(KnowledgeError, match="source_changed_during_index"):
        await dense.search(aid, "热传导", 1)
    assert len(dense.index.vectors) == 0


@pytest.mark.anyio
async def test_source_sha_corruption_never_silently_degrades_or_embeds(
    client,
    postgres_engine,
    recording_minio_storage,
):
    from sqlalchemy import select

    from huipi_cloud.modules.knowledge.models import KnowledgeDocument

    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    async with factory() as session:
        doc = await session.scalar(select(KnowledgeDocument))
        key = doc.object_key
    from io import BytesIO

    await recording_minio_storage.upload_fileobj(BytesIO(b"corrupted"), key, "text/plain")
    fake, _, dense, hybrid = retrievers(factory, recording_minio_storage)
    for retriever in (dense, hybrid):
        with pytest.raises(KnowledgeError, match="sha_mismatch"):
            await retriever.search(aid, "热传导", 1)
    assert fake.documents == [] and not dense.index.vectors


@pytest.mark.anyio
async def test_hybrid_citation_budget_sql_and_forgery_do_not_weaken_existing_boundary(
    client,
    postgres_engine,
    recording_minio_storage,
):
    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid, "热传导通过接触传递能量。" * 70)
    fake, lexical, dense, hybrid = retrievers(factory, recording_minio_storage, budget=2400)
    result = await hybrid.search(aid, "热传导", 5)
    assert result.hits and result.truncated
    assert len(result.model_dump_json().encode()) + 128 <= 2400
    with pytest.raises(KnowledgeError, match="citation_source_stale"):
        bad = result.hits[0].model_copy(update={"chunk_id": uuid4()})
        await hybrid.validate_citations(aid, [bad])
    await hybrid.search(aid, "热传导'; DROP TABLE assignments; --", 1)
    assert (await lexical.search(aid, "热传导", 1)).hits
    assert all(len(s) <= 400 for s in fake.documents)


@pytest.mark.anyio
async def test_agent_real_source_citations_and_degraded_trace(
    client,
    postgres_engine,
    recording_minio_storage,
):
    from huipi_cloud.core.config import Settings

    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    fake, _, _, hybrid = retrievers(factory, recording_minio_storage)
    for failure in (None, "embedding_model_unavailable"):
        fake.fail = failure
        provider = RagProvider()
        runtime = CopilotRuntime(
            provider, BusinessTools(factory, aid, retriever=hybrid), Settings()
        )
        result = await runtime.run(
            CopilotRequest(assignment_id=aid, question="查询热传导", require_material_evidence=True)
        )
        assert result.status == "completed" and result.citations
        events = [e for e in result.trace if e.kind == "tool"]
        assert events[0].degraded == bool(failure)
        assert events[0].retrieval_mode == ("lexical" if failure else "hybrid")


@pytest.mark.anyio
@pytest.mark.parametrize("vectors", [[[1, 2]], [[1, float("nan"), 0, 0]], []])
async def test_actual_invalid_embedding_output_cannot_publish_partial_index(
    client,
    postgres_engine,
    recording_minio_storage,
    vectors,
):
    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    fake, _, dense, hybrid = retrievers(factory, recording_minio_storage)

    async def invalid(texts):
        return vectors

    fake.embed_documents = invalid
    result = await hybrid.search(aid, "热传导", 1)
    assert result.degraded and result.degradation_reason.startswith("embedding_")
    assert not dense.index.vectors and dense.index_status == "not_ready"


@pytest.mark.anyio
async def test_cached_vector_cannot_bypass_late_s3_damage_or_storage_outage(
    client,
    postgres_engine,
    recording_minio_storage,
):
    from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError

    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    fake, _, dense, hybrid = retrievers(factory, recording_minio_storage)
    assert (await hybrid.search(aid, "热传导", 1)).hits
    before = len(fake.documents)

    async def unavailable(key):
        raise StorageUnavailableError("never expose internal address")

    recording_minio_storage.download = unavailable
    with pytest.raises(KnowledgeError, match="knowledge_source_unavailable"):
        await dense.search(aid, "热传导", 1)
    assert len(fake.documents) == before


@pytest.mark.anyio
async def test_prepare_failure_is_not_retried_until_explicit_recovery(
    client,
    postgres_engine,
    recording_minio_storage,
):
    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    fake, _, _, hybrid = retrievers(factory, recording_minio_storage)
    fake.fail = "embedding_out_of_memory"
    await hybrid.prepare(aid)
    assert (await hybrid.search(aid, "热传导", 1)).degraded
    assert len(fake.documents) == 1
    fake.fail = None
    await hybrid.prepare(aid)
    assert not (await hybrid.search(aid, "热传导", 1)).degraded
    assert len(fake.documents) == 2


@pytest.mark.anyio
async def test_concurrent_queries_publish_one_compatible_cache_snapshot(
    client,
    postgres_engine,
    recording_minio_storage,
    monkeypatch,
):
    import asyncio

    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    fake, _, dense, _ = retrievers(factory, recording_minio_storage)
    second_read = asyncio.Event()
    original, calls = dense.verifier._rows, 0

    async def rows(*args, **kwargs):
        nonlocal calls
        value = await original(*args, **kwargs)
        calls += 1
        if calls == 2:
            second_read.set()
        return value

    monkeypatch.setattr(dense.verifier, "_rows", rows)
    fake.after_embed = second_read.wait
    first, second = await asyncio.wait_for(
        asyncio.gather(dense.search(aid, "热传导", 1), dense.search(aid, "导热", 1)), timeout=5
    )
    assert first.hits[0].chunk_id == second.hits[0].chunk_id
    assert len(fake.documents) == 1 and len(dense.index.vectors) == 1


@pytest.mark.anyio
async def test_retrieval_configuration_changes_agent_fingerprint(
    client,
    postgres_engine,
    recording_minio_storage,
):
    from huipi_cloud.core.config import Settings

    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    _, lexical, _, hybrid = retrievers(factory, recording_minio_storage)
    outputs = []
    for retriever in (lexical, hybrid):
        outputs.append(
            await CopilotRuntime(
                RagProvider(), BusinessTools(factory, aid, retriever=retriever), Settings()
            ).run(CopilotRequest(assignment_id=aid, question="查询热传导"))
        )
    assert outputs[0].request_sha256 != outputs[1].request_sha256
    assert all(r.status == "completed" for r in outputs)


@pytest.mark.anyio
async def test_model_identity_change_during_build_does_not_mix_vector_spaces(
    client,
    postgres_engine,
    recording_minio_storage,
):
    factory, aid = await assignment(client, postgres_engine)
    await put(factory, recording_minio_storage, aid)
    fake, _, dense, _ = retrievers(factory, recording_minio_storage)

    async def change():
        fake.identity = replace(fake.identity, model_revision="during-build-change")

    fake.after_embed = change
    with pytest.raises(EmbeddingError, match="embedding_config_changed"):
        await dense.search(aid, "热传导", 1)
    assert not dense.index.vectors
