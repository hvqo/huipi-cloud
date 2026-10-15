"""Read-only dataset validation and transparent ranking metrics; no model in CI."""

import hashlib
import json
import math
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from huipi_cloud.modules.knowledge.chunking import chunk_text


def load_dataset(path):
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if path.with_suffix(path.suffix + ".sha256").read_text().split()[0] != digest:
        raise ValueError("retrieval_dataset_sha_mismatch")
    data = json.loads(raw)
    if data["dataset_version"] != "scoped-retrieval-v1":
        raise ValueError("retrieval_dataset_version_mismatch")
    if data["reference_origin"] != "synthetic_curated_reference":
        raise ValueError("retrieval_reference_origin_invalid")
    docs = {(d["assignment"], d["id"], d["source_version"]): d for d in data["documents"]}
    if len(docs) != len(data["documents"]):
        raise ValueError("retrieval_duplicate_document_version")
    seen = set()
    for case in data["queries"]:
        if case["case_id"] in seen:
            raise ValueError("retrieval_duplicate_case")
        seen.add(case["case_id"])
        if not 1 <= len(case["query"]) <= 250:
            raise ValueError("retrieval_query_limit")
        for ref in case["relevant"]:
            doc = docs[(case["assignment"], ref["document"], ref["source_version"])]
            if doc["status"] != "active" or not 0 <= ref["chunk_index"] < len(
                chunk_text(doc["text"])
            ):
                raise ValueError("retrieval_reference_invalid")
    return data, digest


def ranking_metrics(cases):
    answerable = [c for c in cases if c["relevant"]]
    no_answer = [c for c in cases if not c["relevant"]]
    result = {"answerable_cases": len(answerable), "no_answer_cases": len(no_answer)}
    for k in (1, 3, 5):
        recall_sum = sum(
            len(set(c["returned"][:k]) & set(c["relevant"])) / len(c["relevant"])
            for c in answerable
        )
        relevant_count = sum(len(set(c["returned"][:k]) & set(c["relevant"])) for c in answerable)
        result[f"recall_at_{k}"] = {
            "value": recall_sum / len(answerable) if answerable else None,
            "sum_case_recall": recall_sum,
            "denominator_cases": len(answerable),
        }
        denominator = len(answerable) * k
        result[f"precision_at_{k}"] = {
            "value": relevant_count / denominator if denominator else None,
            "numerator": relevant_count,
            "denominator": denominator,
        }
    reciprocal_sum = sum(
        next((1 / rank for rank, hit in enumerate(c["returned"], 1) if hit in c["relevant"]), 0)
        for c in answerable
    )
    result["mrr"] = {
        "value": reciprocal_sum / len(answerable) if answerable else None,
        "sum_reciprocal_rank": reciprocal_sum,
        "denominator": len(answerable),
    }
    false_hits = sum(bool(c["returned"]) for c in no_answer)
    result["no_answer_false_hit"] = {
        "numerator": false_hits,
        "denominator": len(no_answer),
        "value": false_hits / len(no_answer) if no_answer else None,
    }
    times = sorted(c["elapsed_ms"] for c in cases)
    result["latency_ms"] = {
        f"p{p}": times[max(0, math.ceil(len(times) * p / 100) - 1)] if times else None
        for p in (50, 95)
    }
    result["cross_assignment_leaks"] = sum(c["cross_assignment_leaks"] for c in cases)
    result["stale_hits"] = sum(c["stale_hits"] for c in cases)
    return result


def evaluation_scope_ids(dataset_sha, aliases):
    """Stable only for isolated synthetic evaluation, never a tenant authorization scheme."""
    return {
        alias: uuid5(NAMESPACE_URL, f"huipi:retrieval-evaluation:{dataset_sha}:{alias}")
        for alias in sorted(aliases)
    }
