"""Explicit offline local-model experiment; only own isolated synthetic PG/S3 fixtures."""

import argparse
import asyncio
import json
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4, uuid5

from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.embedding.local import LocalEmbeddingProvider
from huipi_cloud.infrastructure.reranking.local import LocalRerankProvider
from huipi_cloud.infrastructure.reranking.protocol import RerankError
from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.knowledge.chunking import sha256
from huipi_cloud.modules.knowledge.evaluation import evaluation_scope_ids, load_dataset
from huipi_cloud.modules.knowledge.hybrid import DenseRetriever, HybridRetriever, bounded_output
from huipi_cloud.modules.knowledge.models import KnowledgeChunk, KnowledgeDocument
from huipi_cloud.modules.knowledge.rerank import RerankRetriever
from huipi_cloud.modules.knowledge.rerank_evaluation import (
    acceptance,
    paired_changes,
    quality_metrics,
)
from huipi_cloud.modules.knowledge.retriever import LexicalRetriever
from huipi_cloud.modules.knowledge.schemas import ImportRequest
from huipi_cloud.modules.knowledge.service import import_material, revoke_material

ROOT = Path("data/evaluation/retrieval")


@asynccontextmanager
async def corpus(cfg, data, digest):
    url = make_url(cfg.test_database_url or "")
    if (
        not url.database
        or not url.database.endswith("_test")
        or url.database == make_url(cfg.database_url).database
        or cfg.minio_test_bucket == cfg.minio_bucket
    ):
        raise ValueError("isolated_test_services_required")
    engine = create_async_engine(cfg.test_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = S3ObjectStorage(
        endpoint_url=cfg.minio_endpoint_url,
        access_key=cfg.minio_access_key,
        secret_key=cfg.minio_secret_key,
        bucket=cfg.minio_test_bucket,
        object_key_prefix=f"tests/rerank-evaluation/{uuid4()}",
    )
    storage._client.head_bucket(Bucket=storage.bucket)  # tests/operator must initialize test bucket
    scopes = evaluation_scope_ids(digest, {d["assignment"] for d in data["documents"]})
    created = False
    try:
        async with factory() as session, session.begin():
            for alias, aid in scopes.items():
                session.add(
                    Assignment(
                        id=aid,
                        title=f"synthetic-rerank-{alias}",
                        subject="合成",
                        grade_level="高中",
                    )
                )
        created = True
        logical = {}
        for doc in data["documents"]:
            aid = scopes[doc["assignment"]]
            did = uuid5(aid, doc["id"])
            logical[(aid, did)] = doc["id"]
            request = ImportRequest(
                assignment_id=aid,
                document_id=did,
                title=doc["title"],
                source_type="markdown",
                permission="synthetic",
                source_sha256=sha256(doc["text"].encode()),
            )
            result = await import_material(factory, storage, request, doc["text"].encode())
            if result.source_version != doc["source_version"]:
                raise ValueError("source_version_mismatch")
            if doc["status"] == "revoked":
                await revoke_material(factory, aid, did)
        meta = {}
        async with factory() as session:
            rows = (
                await session.execute(
                    select(KnowledgeDocument, KnowledgeChunk)
                    .join(
                        KnowledgeChunk, KnowledgeChunk.document_version_id == KnowledgeDocument.id
                    )
                    .where(KnowledgeDocument.assignment_id.in_(list(scopes.values())))
                )
            ).all()
            for d, c in rows:
                label = (
                    f"{logical[(d.assignment_id, d.document_id)]}:"
                    f"v{d.source_version}:{c.chunk_index}"
                )
                meta[c.id] = (d.assignment_id, d.status, label)
        yield factory, storage, scopes, meta
    finally:
        if created:
            async with factory() as session:
                keys = (
                    await session.scalars(
                        select(KnowledgeDocument.object_key).where(
                            KnowledgeDocument.assignment_id.in_(list(scopes.values()))
                        )
                    )
                ).all()
            for key in keys:
                await storage.delete(key)
            async with factory() as session, session.begin():
                await session.execute(
                    delete(Assignment).where(Assignment.id.in_(list(scopes.values())))
                )
        await engine.dispose()


class Recorder:
    def __init__(self, provider):
        self.provider, self.last = provider, None

    @property
    def identity(self):
        return self.provider.identity

    async def rerank(self, q, candidates):
        self.last = await self.provider.rerank(q, candidates)
        return self.last

    async def warmup(self):
        await self.provider.warmup()

    async def aclose(self):
        await self.provider.aclose()


def own_gpu_memory(providers):
    ids = {str(p.process.pid) for p in providers if p.process}
    try:
        lines = subprocess.check_output(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            text=True,
        ).splitlines()
        return sum(int(row.split(",")[1]) for row in lines if row.split(",")[0].strip() in ids)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


async def evaluate(cfg, data, digest, manifest, manifest_sha):
    gate = asyncio.Semaphore(1)
    embedding = LocalEmbeddingProvider(
        cfg.embedding_python,
        cfg.embedding_model_path,
        device=cfg.embedding_device,
        inference_gate=gate,
    )
    provider = LocalRerankProvider(
        cfg.rerank_python, cfg.rerank_model_path, device=cfg.rerank_device, inference_gate=gate
    )
    recorder = Recorder(provider)
    started = datetime.now(UTC).isoformat()
    records = {m: [] for m in ["lexical", "dense", "hybrid", "dense_rerank", "hybrid_rerank"]}
    gpu_peak = 0
    try:
        async with corpus(cfg, data, digest) as (factory, storage, scopes, meta):
            lexical = LexicalRetriever(factory, storage, max_result_bytes=16384)
            dense = DenseRetriever(
                factory,
                storage,
                embedding,
                max_result_bytes=16384,
                min_cosine=manifest["dense_min_cosine"],
            )
            hybrid = HybridRetriever(lexical, dense, allow_lexical_fallback=False)
            rerankers = {
                mode: RerankRetriever(
                    source,
                    recorder,
                    requested_mode=mode,
                    candidate_limit=manifest["candidate_limit"],
                    min_score=manifest["rerank_min_score"],
                    max_result_bytes=16384,
                )
                for mode, source in [("dense_rerank", dense), ("hybrid_rerank", hybrid)]
            }
            tick = perf_counter()
            for aid in scopes.values():
                await dense.prepare(aid)
            index_seconds = perf_counter() - tick
            tick = perf_counter()
            await provider.warmup()
            load_seconds = perf_counter() - tick
            gpu_peak = own_gpu_memory([embedding, provider])
            if gpu_peak is None and cfg.rerank_device == "cuda":
                raise ValueError("gpu_telemetry_required")
            gpu_peak = gpu_peak or 0
            for case in data["queries"]:
                aid = scopes[case["assignment"]]
                expected = {
                    f"{r['document']}:v{r['source_version']}:{r['chunk_index']}": r["relevance"]
                    for r in case["relevant"]
                }
                snapshots = {}
                for mode, source in [
                    ("lexical", lexical),
                    ("dense", dense),
                    ("hybrid", hybrid),
                    ("dense_rerank", dense),
                    ("hybrid_rerank", hybrid),
                ]:
                    tick = perf_counter()
                    status = "ok"
                    recorder.last = None
                    if mode.endswith("_rerank"):
                        base = mode.removesuffix("_rerank")
                        candidates = snapshots[base]
                        try:
                            hits, metadata = await rerankers[mode].rank_candidates(
                                aid, case["query"], candidates
                            )
                            result = await bounded_output(
                                rerankers[mode],
                                aid,
                                hits,
                                5,
                                upstream_truncated=candidates.truncated,
                                **metadata,
                            )
                        except RerankError as error:
                            status = error.code
                            result = None
                        # Include the measured cost of retrieving this identical candidate snapshot.
                        candidate_ms = records[base][-1]["candidate_elapsed_ms"]
                    else:
                        candidates = await source.retrieve_candidates(aid, case["query"], 20)
                        candidate_ms = (perf_counter() - tick) * 1000
                        snapshots[mode] = candidates
                        metadata = candidates.model_dump(exclude={"hits", "truncated"})
                        verifier = dense.verifier if mode == "dense" else lexical
                        result = await bounded_output(
                            verifier,
                            aid,
                            candidates.hits,
                            5,
                            upstream_truncated=candidates.truncated,
                            **metadata,
                        )
                    elapsed = (perf_counter() - tick) * 1000
                    if mode.endswith("_rerank"):
                        elapsed += candidate_ms
                    hits = result.hits if result else []
                    ids = [meta[h.chunk_id] for h in hits]
                    reply = recorder.last
                    records[mode].append(
                        dict(
                            case_id=case["case_id"],
                            category=case["category"],
                            relevant=list(expected),
                            grades=expected,
                            candidate_labels=[meta[h.chunk_id][2] for h in candidates.hits],
                            candidate_count=len(candidates.hits),
                            candidate_elapsed_ms=round(candidate_ms, 3),
                            returned=[p[2] for p in ids],
                            scores=[h.relevance_score for h in hits],
                            elapsed_ms=round(elapsed, 3),
                            cross_assignment_leaks=sum(p[0] != aid for p in ids),
                            stale_hits=sum(p[1] != "active" for p in ids),
                            status=status,
                            rerank_input_tokens=reply.input_tokens if reply else 0,
                            rerank_model_ms=reply.elapsed_ms if reply else 0,
                            rerank_scores={meta[k][2]: v for k, v in reply.scores.items()}
                            if reply
                            else {},
                        )
                    )
                    if mode.endswith("_rerank"):
                        memory = own_gpu_memory([embedding, provider])
                        if memory is not None:
                            gpu_peak = max(gpu_peak, memory)
            metrics = {mode: quality_metrics(rows) for mode, rows in records.items()}
            return dict(
                report_version="rerank-quality-v1",
                synthetic_only=True,
                human_review_status="unreviewed",
                started_at=started,
                completed_at=datetime.now(UTC).isoformat(),
                dataset_sha256=digest,
                manifest_sha256=manifest_sha,
                code_commit=subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
                code_sha256={
                    str(p): sha256(p.read_bytes())
                    for root in [
                        Path("src/huipi_cloud/modules/knowledge"),
                        Path("src/huipi_cloud/infrastructure/embedding"),
                        Path("src/huipi_cloud/infrastructure/reranking"),
                    ]
                    for p in sorted(root.glob("*.py"))
                },
                embedding_identity=embedding.identity.__dict__,
                rerank_identity=provider.identity.__dict__,
                index_seconds=round(index_seconds, 3),
                rerank_load_seconds=round(load_seconds, 3),
                vector_count=len(dense.index.vectors),
                vector_payload_bytes=dense.index.payload_bytes,
                gpu_peak_observed_mib=gpu_peak,
                metrics=metrics,
                cases=records,
                paired={m: paired_changes(records["dense"], records[m]) for m in rerankers},
                acceptance=acceptance(records, metrics, gpu_peak, manifest["acceptance"]),
                source_guard_test_status="verify_default_test_suite_and_CI_separately",
                limitations=[
                    "synthetic_labels_not_teacher_double_review",
                    "GPU_polling_not_hard_memory_limit",
                    "rerank_latency_reuses_measured_identical_candidate_snapshot_cost",
                    "no_default_change_allowed_without_independent_review",
                ],
            )
    finally:
        await provider.aclose()
        await embedding.aclose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=ROOT / "rerank-holdout-v1.json")
    parser.add_argument("--run-local", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    version = (
        "rerank-holdout-v1"
        if args.dataset.name == "rerank-holdout-v1.json"
        else "scoped-retrieval-v1"
    )
    data, digest = load_dataset(args.dataset, expected_version=version)
    path = ROOT / "rerank-experiment-v1.json"
    raw = path.read_bytes()
    if sha256(raw) != path.with_suffix(".json.sha256").read_text().split()[0]:
        raise ValueError("manifest_sha_mismatch")
    manifest = json.loads(raw)
    if version == "rerank-holdout-v1" and manifest["dataset_sha256"] != digest:
        raise ValueError("dataset_manifest_mismatch")
    if not args.run_local:
        print(
            json.dumps(
                dict(status="dry_run", cases=len(data["queries"]), sha256=digest, paid_requests=0)
            )
        )
        return
    if os.getenv("RUN_LOCAL_RERANK") != "1" or args.output is None or args.output.exists():
        raise ValueError("explicit_opt_in_and_new_output_required")
    report = asyncio.run(evaluate(Settings(), data, digest, manifest, sha256(raw)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    args.output.with_suffix(".json.sha256").write_text(
        sha256(args.output.read_bytes()) + "  " + args.output.name + "\n"
    )
    print(
        json.dumps(
            {
                m: {k: v for k, v in metrics.items() if k not in ["rerank_candidate_counts"]}
                for m, metrics in report["metrics"].items()
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
