"""Explicit local-only real-PG/S3 retrieval benchmark. No paid API or default weight download."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4, uuid5

from botocore.exceptions import ClientError
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.embedding.local import LocalEmbeddingProvider
from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage
from huipi_cloud.modules.assignments.models import Assignment
from huipi_cloud.modules.knowledge.chunking import sha256
from huipi_cloud.modules.knowledge.evaluation import load_dataset, ranking_metrics
from huipi_cloud.modules.knowledge.hybrid import DenseRetriever, HybridRetriever
from huipi_cloud.modules.knowledge.models import KnowledgeChunk, KnowledgeDocument
from huipi_cloud.modules.knowledge.retriever import LexicalRetriever
from huipi_cloud.modules.knowledge.schemas import ImportRequest
from huipi_cloud.modules.knowledge.service import import_material, revoke_material

DEFAULT_DATASET = Path("data/evaluation/retrieval/scoped-cases-v1.json")


async def evaluate(args, data, digest):
    if os.getenv("RUN_LOCAL_EMBEDDING") != "1":
        raise ValueError("explicit_RUN_LOCAL_EMBEDDING_required")
    cfg = Settings()
    if (
        not cfg.test_database_url
        or not make_url(cfg.test_database_url).database.endswith("_test")
        or make_url(cfg.test_database_url).database == make_url(cfg.database_url).database
    ):
        raise ValueError("independent_test_database_required")
    if cfg.minio_test_bucket == cfg.minio_bucket:
        raise ValueError("independent_test_bucket_required")
    engine = create_async_engine(cfg.test_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = S3ObjectStorage(
        endpoint_url=cfg.minio_endpoint_url,
        access_key=cfg.minio_access_key,
        secret_key=cfg.minio_secret_key,
        bucket=cfg.minio_test_bucket,
        object_key_prefix=f"tests/hybrid-evaluation/{uuid4()}",
    )
    try:
        storage._client.head_bucket(Bucket=storage.bucket)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        storage._client.create_bucket(Bucket=storage.bucket)
    embedding = LocalEmbeddingProvider(
        cfg.embedding_python,
        cfg.embedding_model_path,
        device=cfg.embedding_device,
        timeout=cfg.embedding_timeout_seconds,
    )
    lexical = LexicalRetriever(factory, storage, max_result_bytes=16384)
    dense = DenseRetriever(
        factory, storage, embedding, min_cosine=cfg.embedding_min_cosine, max_result_bytes=16384
    )
    hybrid = HybridRetriever(lexical, dense, allow_lexical_fallback=False)
    scopes = {alias: uuid4() for alias in {d["assignment"] for d in data["documents"]}}
    keys, logical_ids, metadata = [], {}, {}
    started = datetime.now(UTC).isoformat()
    try:
        async with factory() as session, session.begin():
            for alias, aid in scopes.items():
                session.add(
                    Assignment(
                        id=aid,
                        title=f"synthetic-retrieval-{alias}",
                        subject="合成",
                        grade_level="高中",
                    )
                )
        for doc in data["documents"]:
            aid = scopes[doc["assignment"]]
            did = uuid5(aid, doc["id"])
            logical_ids[(aid, did)] = doc["id"]
            request = ImportRequest(
                assignment_id=aid,
                document_id=did,
                title=doc["title"],
                source_type="markdown",
                permission="synthetic",
                source_sha256=sha256(doc["text"].encode()),
            )
            receipt = await import_material(factory, storage, request, doc["text"].encode())
            if receipt.source_version != doc["source_version"]:
                raise ValueError("retrieval_import_version_mismatch")
            if doc["status"] == "revoked":
                await revoke_material(factory, aid, did)
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
            for doc, chunk in rows:
                label = (
                    f"{logical_ids[(doc.assignment_id, doc.document_id)]}:"
                    f"v{doc.source_version}:{chunk.chunk_index}"
                )
                metadata[str(chunk.id)] = (doc.assignment_id, doc.status, label)
                keys.append(doc.object_key)
        index_started = perf_counter()
        for aid in scopes.values():
            await dense.prepare(aid)
        index_seconds = perf_counter() - index_started
        results, records = {}, {}
        for mode, retriever in (("lexical", lexical), ("dense", dense), ("hybrid", hybrid)):
            records[mode] = []
            for case in data["queries"]:
                aid, tick = scopes[case["assignment"]], perf_counter()
                result = await retriever.search(aid, case["query"], 5)
                relevant = [
                    f"{r['document']}:v{r['source_version']}:{r['chunk_index']}"
                    for r in case["relevant"]
                ]
                ids = [metadata[str(h.chunk_id)] for h in result.hits]
                records[mode].append(
                    dict(
                        case_id=case["case_id"],
                        category=case["category"],
                        relevant=relevant,
                        returned=[p[2] for p in ids],
                        scores=[h.relevance_score for h in result.hits],
                        elapsed_ms=round((perf_counter() - tick) * 1000, 3),
                        cross_assignment_leaks=sum(p[0] != aid for p in ids),
                        stale_hits=sum(p[1] != "active" for p in ids),
                        actual_mode=result.retrieval_mode,
                        degraded=result.degraded,
                    )
                )
            results[mode] = ranking_metrics(records[mode])
        return dict(
            report_version="hybrid-retrieval-v1",
            synthetic_only=True,
            human_review_status="unreviewed",
            started_at=started,
            completed_at=datetime.now(UTC).isoformat(),
            dataset_sha256=digest,
            freeze_commit=subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
            model_identity=embedding.identity.__dict__,
            device=cfg.embedding_device,
            embedding_config_digest=embedding.identity.digest,
            index_seconds=round(index_seconds, 3),
            vector_count=len(dense.index.vectors),
            vector_float32_payload_bytes=dense.index.payload_bytes,
            metrics=results,
            cases=records,
            limitations=[
                "synthetic_not_teacher_double_review",
                "no_large_scale_ANN",
                "min_cosine_not_calibrated",
                "no_semantic_answer_correctness_claim",
            ],
        )
    finally:
        await embedding.aclose()
        # Query our own rows even if import/index failed. Never touch dev data/bucket/other keys.
        async with factory() as session:
            own_keys = (
                await session.scalars(
                    select(KnowledgeDocument.object_key).where(
                        KnowledgeDocument.assignment_id.in_(list(scopes.values()))
                    )
                )
            ).all()
        for key in set(keys) | set(own_keys):
            await storage.delete(key)
        async with factory() as session, session.begin():
            await session.execute(
                delete(Assignment).where(Assignment.id.in_(list(scopes.values())))
            )
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--run-local", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data, digest = load_dataset(args.dataset)
    manifest_path = args.dataset.with_name("experiment-v1.json")
    manifest_raw = manifest_path.read_bytes()
    assert (
        hashlib.sha256(manifest_raw).hexdigest()
        == manifest_path.with_suffix(".json.sha256").read_text().split()[0]
    ), "experiment_manifest_sha_mismatch"
    manifest = json.loads(manifest_raw)
    if manifest["dataset_sha256"] != digest:
        raise ValueError("experiment_dataset_sha_mismatch")
    if not args.run_local:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "cases": len(data["queries"]),
                    "dataset_sha256": digest,
                    "paid_requests": 0,
                }
            )
        )
        return
    if args.output is None or args.output.exists():
        raise ValueError("new_output_path_required")
    if Settings().embedding_min_cosine != manifest["min_cosine"]:
        raise ValueError("experiment_config_mismatch")
    result = asyncio.run(evaluate(args, data, digest))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        sha256(args.output.read_bytes()) + "  " + args.output.name + "\n"
    )
    print(json.dumps(result["metrics"], ensure_ascii=False))


if __name__ == "__main__":
    main()
