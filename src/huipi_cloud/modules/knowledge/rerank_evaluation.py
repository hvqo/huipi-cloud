"""Ranking quality and pre-registered acceptance; source identity is not answer semantics."""

import math

from huipi_cloud.modules.knowledge.evaluation import ranking_metrics


def quality_metrics(cases):
    result = ranking_metrics(cases)
    answerable = [c for c in cases if c["relevant"]]
    for k in (10, 20):
        total = sum(
            len(set(c["candidate_labels"][:k]) & set(c["relevant"])) / len(c["relevant"])
            for c in answerable
        )
        result[f"candidate_recall_at_{k}"] = dict(
            sum_case_recall=total,
            denominator_cases=len(answerable),
            value=total / len(answerable) if answerable else None,
        )
    for k in (1, 3, 5):
        values = []
        for case in answerable:

            def gain(labels):
                return sum(
                    (2 ** case["grades"].get(label, 0) - 1) / math.log2(i + 2)
                    for i, label in enumerate(labels[:k])
                )

            ideal = sorted(case["grades"], key=lambda key: -case["grades"][key])
            denominator = gain(ideal)
            values.append(gain(case["returned"]) / denominator if denominator else 0)
        result[f"ndcg_at_{k}"] = dict(
            sum_ndcg=sum(values),
            denominator=len(values),
            value=sum(values) / len(values) if values else None,
        )
    result["rerank_errors"] = sum(c["status"] != "ok" for c in cases)
    result["rerank_candidate_counts"] = [c["candidate_count"] for c in cases]
    result["rerank_input_tokens"] = sum(c["rerank_input_tokens"] for c in cases)
    result["candidate_miss_case_ids"] = [
        c["case_id"] for c in answerable if not set(c["candidate_labels"]) & set(c["relevant"])
    ]
    result["present_but_not_first_case_ids"] = [
        c["case_id"]
        for c in answerable
        if set(c["candidate_labels"]) & set(c["relevant"])
        and (not c["returned"] or c["returned"][0] not in c["relevant"])
    ]
    return result


def paired_changes(before, after):
    if [c["case_id"] for c in before] != [c["case_id"] for c in after]:
        raise ValueError("rerank_case_set_mismatch")
    rows = []
    for a, b in zip(before, after):
        correct_a = bool(a["returned"]) and a["returned"][0] in a["relevant"]
        correct_b = bool(b["returned"]) and b["returned"][0] in b["relevant"]
        if a["returned"] != b["returned"]:
            rows.append(
                dict(
                    case_id=a["case_id"],
                    before=a["returned"],
                    after=b["returned"],
                    top1_before=correct_a,
                    top1_after=correct_b,
                )
            )
    return rows


def acceptance(records, metrics, observed_gpu_mib, criteria):
    dense = metrics["dense"]
    baseline_correct = {
        c["case_id"]
        for c in records["dense"]
        if c["returned"] and c["returned"][0] in c["relevant"]
    }
    output = {}
    for mode, base in (("dense_rerank", "dense"), ("hybrid_rerank", "hybrid")):
        measured = metrics[mode]
        kept = sum(
            c["case_id"] in baseline_correct
            and bool(c["returned"])
            and c["returned"][0] in c["relevant"]
            for c in records[mode]
        )
        retention = kept / len(baseline_correct) if baseline_correct else 0
        checks = dict(
            scope_and_version_safe=measured["cross_assignment_leaks"] == 0
            and measured["stale_hits"] == 0,
            candidate_coverage=measured["candidate_recall_at_20"]["value"]
            >= criteria["candidate_recall_at_20_min"],
            no_answer=measured["no_answer_false_hit"]["numerator"]
            <= dense["no_answer_false_hit"]["numerator"],
            recall=measured["recall_at_1"]["value"] >= dense["recall_at_1"]["value"] - 0.02,
            dense_correct_retention=retention >= criteria["dense_correct_top1_retention_min"],
            ndcg=measured["ndcg_at_5"]["value"] >= dense["ndcg_at_5"]["value"] - 0.02,
            latency=measured["latency_ms"]["p95"] <= criteria["warm_p95_ms_max"],
            gpu=observed_gpu_mib <= criteria["own_gpu_memory_mib_max"],
            reranker_errors=measured["rerank_errors"] == 0,
        )
        output[mode] = dict(
            checks=checks,
            empirical_checks_pass=all(checks.values()),
            dense_top1_preserved=dict(
                numerator=kept, denominator=len(baseline_correct), value=retention
            ),
            source_validation_guard="requires_automated_fault_tests_not_inferred_from_quality_data",
            human_review_status="unreviewed",
            production_default_change_allowed=False,
        )
    return output
