"""Numerical/identity tests never download weights or call a paid service."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.embedding.local import LocalEmbeddingProvider
from huipi_cloud.infrastructure.embedding.protocol import (
    EmbeddingError,
    EmbeddingIdentity,
    normalize,
)
from huipi_cloud.modules.knowledge.dependencies import configured_retriever
from huipi_cloud.modules.knowledge.hybrid import rrf
from huipi_cloud.modules.knowledge.retriever import LexicalRetriever
from huipi_cloud.modules.knowledge.vector_index import MemoryVectorIndex, VectorKey


class FakeEmbedding:
    def __init__(self):
        self.identity = EmbeddingIdentity(
            model_id="synthetic-fake", model_revision="test-v1", dimension=4
        )
        self.documents = []
        self.queries = []
        self.fail = None
        self.after_embed = None

    @staticmethod
    def vector(text):
        if any(t in text for t in ("热传导", "接触", "导热")):
            return [1, 0, 0, 0]
        if any(t in text for t in ("光合", "photosynthesis")):
            return [0, 1, 0, 0]
        return [0, 0, 1, 0]

    async def embed_documents(self, texts):
        self.documents.extend(texts)
        if self.fail:
            raise EmbeddingError(self.fail)
        result = [self.vector(t) for t in texts]
        if self.after_embed:
            await self.after_embed()
        return result

    async def embed_query(self, text):
        self.queries.append(text)
        if self.fail:
            raise EmbeddingError(self.fail)
        return self.vector(text)

    async def aclose(self):
        pass


@pytest.mark.parametrize(
    "vector,code",
    [
        ([1], "dimension_mismatch"),
        ([True, 0], "non_finite_vector"),
        ([float("nan"), 0], "non_finite_vector"),
        ([float("inf"), 0], "non_finite_vector"),
        ([0, 0], "invalid_norm"),
        ([1e308, 1e308], None),
    ],
)
def test_vectors_reject_wrong_dimension_non_finite_and_zero(vector, code):
    if code:
        with pytest.raises(EmbeddingError, match=code):
            normalize(vector, 2)
    else:
        assert sum(v * v for v in normalize(vector, 2)) == pytest.approx(1)


def test_identity_digest_changes_and_cache_cannot_mix_versions_dimension_or_source():
    identity = EmbeddingIdentity(dimension=4)
    doc, chunk = (
        SimpleNamespace(source_version=1),
        SimpleNamespace(id=uuid4(), chunk_text_sha256="a" * 64),
    )
    aid = uuid4()
    first = VectorKey.for_chunk(aid, doc, chunk, identity)
    changes = [
        replace(identity, dimension=3),
        replace(identity, model_revision="new"),
        replace(identity, max_tokens=256),
        replace(identity, query_instruction="new"),
    ]
    assert all(i.digest != identity.digest for i in changes)
    keys = [VectorKey.for_chunk(aid, doc, chunk, i) for i in changes]
    keys += [VectorKey.for_chunk(uuid4(), doc, chunk, identity)]
    chunk.chunk_text_sha256 = "b" * 64
    keys += [VectorKey.for_chunk(aid, doc, chunk, identity)]
    doc.source_version = 2
    keys += [VectorKey.for_chunk(aid, doc, chunk, identity)]
    assert all(k != first for k in keys)
    index = MemoryVectorIndex(capacity=2)
    index.publish({first: [1, 0, 0, 0]})
    index.publish({first: [1, 0, 0, 0]})
    assert len(index.vectors) == 1 and index.payload_bytes == 16
    assert all(index.get(k) is None for k in keys)
    with pytest.raises(EmbeddingError):
        index.publish({keys[0]: [1, 2, 3, 4]})
    assert list(index.vectors) == [first]  # failed publication changes nothing


def test_rrf_formula_deduplication_and_deterministic_ties():
    result = rrf([["a", "a", "b"], ["b", "c"]])
    assert result == [
        ("b", pytest.approx(1 / 63 + 1 / 61)),
        ("a", pytest.approx(1 / 61)),
        ("c", pytest.approx(1 / 62)),
    ]
    assert rrf([["z"], ["a"]]) == [("a", 1 / 61), ("z", 1 / 61)]


def test_stable_default_is_lexical_and_explicit_hybrid_has_no_remote_embedding():
    cfg = Settings(_env_file=None)
    assert isinstance(configured_retriever(None, None, cfg), LexicalRetriever)
    hybrid = configured_retriever(None, None, cfg.model_copy(update={"retrieval_mode": "hybrid"}))
    assert isinstance(hybrid.dense.embedding, LocalEmbeddingProvider)


@pytest.mark.anyio
async def test_missing_model_and_invalid_input_are_safe_and_do_not_launch_process():
    provider = LocalEmbeddingProvider("/missing/python", "/missing/model")
    with pytest.raises(EmbeddingError, match="model_unavailable"):
        await provider.embed_query("合成问题")
    with pytest.raises(EmbeddingError, match="input_limit"):
        await provider.embed_documents(["x"] * 9)
    assert provider.process is None
    await provider.aclose()


@pytest.mark.anyio
async def test_embedding_timeout_and_cancellation_kill_owned_subprocess(monkeypatch):
    provider = LocalEmbeddingProvider("/missing", "/missing", timeout=0.05)
    started = asyncio.Event()

    async def start():
        provider.process = await asyncio.create_subprocess_exec(
            "/bin/cat",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        started.set()

    async def blocked_read(timeout):
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "_start", start)
    monkeypatch.setattr(provider, "_read", blocked_read)
    execution = asyncio.create_task(provider.embed_query("合成文本"))
    await started.wait()
    process = provider.process
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, 2)
    assert process.returncode is not None and provider.process is None

    async def timeout_read(timeout):
        raise TimeoutError

    monkeypatch.setattr(provider, "_read", timeout_read)
    with pytest.raises(EmbeddingError, match="embedding_timeout"):
        await provider.embed_query("合成文本")
    assert provider.process is None


def test_frozen_dataset_and_metrics_denominators_do_not_confuse_no_answer(tmp_path):
    import json
    from pathlib import Path

    from huipi_cloud.modules.knowledge.evaluation import load_dataset, ranking_metrics

    path = Path("data/evaluation/retrieval/scoped-cases-v1.json")
    data, digest = load_dataset(path)
    assert len(data["queries"]) == 48 and data["human_review_status"] == "unreviewed"
    manifest = json.loads(path.with_name("experiment-v1.json").read_text())
    assert manifest["dataset_sha256"] == digest
    bad = tmp_path / path.name
    bad.write_bytes(path.read_bytes() + b" ")
    bad.with_suffix(".json.sha256").write_text(digest)
    with pytest.raises(ValueError, match="sha_mismatch"):
        load_dataset(bad)
    cases = [
        dict(
            relevant=["a"],
            returned=["x", "a"],
            elapsed_ms=10,
            cross_assignment_leaks=0,
            stale_hits=0,
        ),
        dict(relevant=[], returned=["x"], elapsed_ms=20, cross_assignment_leaks=1, stale_hits=0),
    ]
    result = ranking_metrics(cases)
    assert result["recall_at_1"]["value"] == 0
    assert result["recall_at_3"]["value"] == 1
    assert result["precision_at_3"] == {"value": 1 / 3, "numerator": 1, "denominator": 3}
    assert result["mrr"]["value"] == 0.5
    assert result["no_answer_false_hit"]["numerator"] == 1
    assert result["latency_ms"] == {"p50": 10, "p95": 20}


@pytest.mark.anyio
async def test_database_failure_never_triggers_embedding_or_remote_fallback():
    from huipi_cloud.modules.knowledge.hybrid import DenseRetriever, HybridRetriever
    from huipi_cloud.modules.knowledge.schemas import KnowledgeError

    fake = FakeEmbedding()
    dense = DenseRetriever(None, None, fake)
    for retriever in (dense, HybridRetriever(LexicalRetriever(None, None), dense)):
        with pytest.raises(KnowledgeError, match="knowledge_database_unavailable"):
            await retriever.search(uuid4(), "合成问题", 1)
    assert fake.documents == fake.queries == []


@pytest.mark.anyio
async def test_final_false_truncation_flag_is_included_in_exact_byte_budget():
    from huipi_cloud.modules.knowledge.hybrid import bounded_output
    from huipi_cloud.modules.knowledge.schemas import KnowledgeError, RetrievalHit, SearchResult

    chunk_id = uuid4()
    hit = RetrievalHit(
        citation_id="K" + chunk_id.hex,
        document_id=uuid4(),
        title="合成",
        source_version=1,
        source_sha256="a" * 64,
        chunk_id=chunk_id,
        chunk_text_sha256="b" * 64,
        text_start=0,
        text_end=1,
        excerpt="字",
        relevance_score=1,
    )

    class Verifier:
        max_result_bytes = (
            len(SearchResult(hits=[hit], truncated=True).model_dump_json().encode()) + 128
        )

        async def validate_citations(self, aid, hits):
            pass

    verifier = Verifier()
    # false is one byte longer than true; the shorter intermediate form cannot set the budget.
    with pytest.raises(KnowledgeError, match="budget_too_small"):
        await bounded_output(verifier, uuid4(), [hit], 1)
    verifier.max_result_bytes += 1
    result = await bounded_output(verifier, uuid4(), [hit], 1)
    assert len(result.model_dump_json().encode()) + 128 == verifier.max_result_bytes
