"""No weights, paid services, or vector database are used by default."""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from huipi_cloud.infrastructure.reranking.local import LocalRerankProvider
from huipi_cloud.infrastructure.reranking.protocol import (
    RerankError,
    RerankIdentity,
    RerankReply,
    validate_candidates,
    validate_reply,
)
from huipi_cloud.modules.knowledge.schemas import RetrievalHit


def hit(text="合成材料"):
    cid = uuid4()
    return RetrievalHit(
        citation_id="K" + cid.hex,
        document_id=uuid4(),
        source_version=1,
        source_sha256="a" * 64,
        chunk_id=cid,
        chunk_text_sha256="b" * 64,
        title="合成资料",
        text_start=0,
        text_end=len(text),
        excerpt=text,
        relevance_score=0.9,
    )


class FakeReranker:
    def __init__(self):
        self.identity = RerankIdentity(model_id="synthetic-reranker", model_revision="test-v1")
        self.calls = []
        self.error = None
        self.after_score = None
        self.scores = None

    async def warmup(self):
        if self.error:
            raise RerankError(self.error)

    async def rerank(self, query, candidates):
        self.calls.append([c.chunk_id for c in candidates])
        if self.error:
            raise RerankError(self.error)
        scores = self.scores or {c.chunk_id: 0.8 for c in candidates}
        if self.after_score:
            await self.after_score()
        return RerankReply(
            scores,
            self.identity.model_id,
            self.identity.model_revision,
            self.identity.digest,
            len(candidates),
            3,
            50,
        )

    async def aclose(self):
        pass


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1, True, "0.5"])
def test_non_finite_or_invalid_relevance_scores_are_rejected(score):
    c, identity = hit(), RerankIdentity()
    reply = RerankReply(
        {c.chunk_id: score}, identity.model_id, identity.model_revision, identity.digest, 1, 1
    )
    with pytest.raises(RerankError, match="invalid_score"):
        validate_reply(reply, [c], identity)


def test_candidate_identity_contract_and_model_snapshot_are_strict():
    a, b = hit(), hit()
    with pytest.raises(RerankError, match="duplicate"):
        validate_candidates("问题", [a, a])
    with pytest.raises(RerankError, match="input_limit"):
        validate_candidates("问题", [hit() for _ in range(21)])
    with pytest.raises(RerankError, match="invalid_candidate"):
        validate_candidates("问题", [a.model_copy(update={"citation_id": "invalid"})])
    identity = RerankIdentity()
    reply = RerankReply(
        {a.chunk_id: 0.5}, identity.model_id, identity.model_revision, identity.digest, 1, 1
    )
    with pytest.raises(RerankError, match="candidate_mismatch"):
        validate_reply(reply, [b], identity)
    for change in ({"model_revision": "new"}, {"max_tokens": 512}, {"instruction": "new"}):
        changed = replace(identity, **change)
        assert changed.digest != identity.digest
        with pytest.raises(RerankError, match="model_mismatch"):
            validate_reply(reply, [a], changed)


@pytest.mark.anyio
async def test_missing_local_model_has_no_remote_fallback():
    provider = LocalRerankProvider("/missing/python", "/missing/model")
    with pytest.raises(RerankError, match="model_unavailable"):
        await provider.rerank("问题", [hit()])
    assert provider.process is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code",
    [
        "rerank_out_of_memory",
        "rerank_token_limit",
        "rerank_model_shape_mismatch",
        "rerank_invalid_output",
    ],
)
async def test_process_failure_types_clean_up_without_raw_content(monkeypatch, code):
    provider = LocalRerankProvider("/missing", "/missing")

    async def start():
        provider.process = await asyncio.create_subprocess_exec(
            "/bin/cat",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    async def read(timeout):
        raise RerankError(code)

    monkeypatch.setattr(provider, "_start", start)
    monkeypatch.setattr(provider, "_read", read)
    with pytest.raises(RerankError, match=code):
        await provider.rerank("问题", [hit()])
    assert provider.process is None


@pytest.mark.anyio
async def test_shared_gpu_gate_serializes_embedding_and_rerank_and_cancel_releases(monkeypatch):
    from huipi_cloud.infrastructure.embedding.local import LocalEmbeddingProvider

    gate = asyncio.Semaphore(1)
    embedding = LocalEmbeddingProvider("/missing", "/missing", inference_gate=gate)
    reranker = LocalRerankProvider("/missing", "/missing", inference_gate=gate, timeout=0.05)
    embedding_started, rerank_started = asyncio.Event(), asyncio.Event()

    async def e_start():
        embedding.process = await asyncio.create_subprocess_exec(
            "/bin/cat",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        embedding_started.set()

    async def r_start():
        reranker.process = await asyncio.create_subprocess_exec(
            "/bin/cat",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        rerank_started.set()

    async def blocked(timeout):
        await asyncio.Event().wait()

    monkeypatch.setattr(embedding, "_start", e_start)
    monkeypatch.setattr(embedding, "_read", blocked)
    monkeypatch.setattr(reranker, "_start", r_start)
    monkeypatch.setattr(reranker, "_read", blocked)
    first = asyncio.create_task(embedding.embed_query("问题"))
    await embedding_started.wait()
    second = asyncio.create_task(reranker.rerank("问题", [hit()]))
    assert reranker.process is None
    process = embedding.process
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first, 2)
    assert process.returncode is not None
    await asyncio.wait_for(rerank_started.wait(), 2)
    with pytest.raises(RerankError, match="rerank_timeout"):
        await asyncio.wait_for(second, 2)
    assert reranker.process is None and not gate.locked()


@pytest.mark.anyio
async def test_active_rerank_cancellation_reaps_owned_process(monkeypatch):
    provider = LocalRerankProvider("/missing", "/missing")
    ready = asyncio.Event()

    async def start():
        provider.process = await asyncio.create_subprocess_exec(
            "/bin/cat",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        ready.set()

    async def read(timeout):
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "_start", start)
    monkeypatch.setattr(provider, "_read", read)
    execution = asyncio.create_task(provider.rerank("问题", [hit()]))
    await ready.wait()
    process = provider.process
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, 2)
    assert process.returncode is not None and provider.process is None


def test_holdout_versions_sha_and_graded_metrics_are_independent_of_predictions(tmp_path):
    import json
    from pathlib import Path

    from huipi_cloud.modules.knowledge.evaluation import load_dataset
    from huipi_cloud.modules.knowledge.rerank_evaluation import quality_metrics

    p = Path("data/evaluation/retrieval/rerank-holdout-v1.json")
    data, digest = load_dataset(p, expected_version="rerank-holdout-v1")
    assert len(data["queries"]) == 60 and sum(bool(c["relevant"]) for c in data["queries"]) == 45
    manifest = json.loads(p.with_name("rerank-experiment-v1.json").read_text())
    assert manifest["dataset_sha256"] == digest
    with pytest.raises(ValueError, match="version_mismatch"):
        load_dataset(p)
    altered = tmp_path / p.name
    altered.write_bytes(p.read_bytes() + b" ")
    altered.with_suffix(".json.sha256").write_text(digest)
    with pytest.raises(ValueError, match="sha_mismatch"):
        load_dataset(altered, expected_version="rerank-holdout-v1")
    rows = [
        dict(
            case_id="two",
            relevant=["a", "b"],
            grades={"a": 2, "b": 1},
            candidate_labels=["x", "a", "b"],
            candidate_count=3,
            returned=["b", "a"],
            elapsed_ms=10,
            cross_assignment_leaks=0,
            stale_hits=0,
            status="ok",
            rerank_input_tokens=99,
        )
    ]
    metric = quality_metrics(rows)
    assert metric["candidate_recall_at_20"]["value"] == 1
    assert metric["recall_at_1"]["value"] == 0.5  # two relevant chunks, not binary hit rate
    assert metric["precision_at_1"]["value"] == 1
    assert metric["mrr"]["value"] == 1
    assert metric["ndcg_at_3"]["value"] == pytest.approx(
        (1 + 3 / 1.5849625007) / (3 + 1 / 1.5849625007)
    )


def test_rerank_worker_never_calls_generate_or_loads_remote_code():
    import ast
    from pathlib import Path

    worker = Path("src/huipi_cloud/infrastructure/reranking/worker.py").read_text()
    nodes = ast.walk(ast.parse(worker))
    assert not any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "generate"
        for n in nodes
    )
    assert "trust_remote_code=False" in worker and "local_files_only=True" in worker


def test_saved_real_cloud_observation_replays_fixed_whitelist_contract_without_paid_calls():
    import hashlib
    import json
    from pathlib import Path
    from uuid import UUID

    from huipi_cloud.modules.copilot.protocol import CopilotResult
    from tests.integration.test_rerank_cloud_e2e import assert_cloud_observations

    p = Path("docs/evaluation/rerank-cloud-v1-observation.json")
    assert (
        hashlib.sha256(p.read_bytes()).hexdigest()
        == p.with_suffix(".json.sha256").read_text().split()[0]
    )
    data = json.loads(p.read_text())
    results = [CopilotResult.model_validate(r) for r in data["responses"]]
    assert data["synthetic_only"] and data["human_review_status"] == "unreviewed"
    # Receipt equality passed in the live test before its overly restrictive tool assertion failed.
    assert_cloud_observations(results, UUID("da8dd0a1-4e4d-5725-a0ef-db3e063f060f"))
    assert sum(r.model_calls for r in results) == 4


def test_cloud_leakage_check_distinguishes_relevance_decimal_from_foreign_period():
    import json
    from pathlib import Path
    from uuid import UUID

    from huipi_cloud.modules.copilot.protocol import CopilotResult
    from tests.integration.test_rerank_cloud_e2e import assert_cloud_observations

    data = json.loads(Path("docs/evaluation/rerank-cloud-v1-observation.json").read_text())
    results = [CopilotResult.model_validate(r) for r in data["responses"]]
    receipt = UUID("da8dd0a1-4e4d-5725-a0ef-db3e063f060f")
    assert_cloud_observations(results, receipt)  # 0.99999 relevance is not a 999-minute fact
    for foreign in ["周期为999分钟。", "标本为红色鲸鱼。"]:
        with pytest.raises(AssertionError):
            assert_cloud_observations(
                [
                    results[0].model_copy(update={"answer": results[0].answer + foreign}),
                    *results[1:],
                ],
                receipt,
            )
