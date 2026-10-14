"""Offline replay of frozen experiments; no API calls and no automatic human approval."""

import json

import pytest

from scripts.summarize_rubric_semantics import DATA, build_summary, locked_dataset, pair


def test_frozen_holdout_reference_version_sha_and_curated_provenance():
    ds, digest = locked_dataset(
        DATA / "rubric-cases-v4-semantics.json",
        DATA / "rubric-cases-v4-semantics-manifest.json",
        DATA / "rubric-semantics-acceptance-v1.json",
    )
    assert digest == "3fbfbf97d8fb2495b9dd5ebb02f17809877a50a86553accfad17b3e595753c9d"
    assert ds.dataset_version == "4.0" and len(ds.cases) == 22
    assert sum(c.reference_status == "included" for c in ds.cases) == 20
    assert all(c.label_provenance == "synthetic_curated_reference" for c in ds.cases)


def test_modified_dataset_cannot_use_frozen_sidecar_or_manifest(tmp_path):
    original = DATA / "rubric-cases-v4-semantics.json"
    changed = tmp_path / original.name
    changed.write_bytes(original.read_bytes() + b" ")
    changed.with_suffix(".json.sha256").write_bytes(
        original.with_suffix(".json.sha256").read_bytes()
    )
    with pytest.raises(ValueError, match="frozen_sha_mismatch"):
        locked_dataset(
            changed,
            DATA / "rubric-cases-v4-semantics-manifest.json",
            DATA / "rubric-semantics-acceptance-v1.json",
        )


def test_paired_criterion_changes_are_not_hidden_by_same_total():
    a = {"cases": [{"case_id": "same", "model_total": "10.00", "all_scores_exact": True}]}
    b = {"cases": [{"case_id": "same", "model_total": "10.00", "all_scores_exact": False}]}
    p = pair(a, b)
    assert p[0]["new_error"] is True and p[0]["recovered"] is False
    with pytest.raises(ValueError, match="case_set"):
        pair(a, {"cases": []})


def test_raw_model_replay_preserves_failures_and_does_not_promote_semantic_review():
    summary, checklist = build_summary()
    assert summary["new_provider_requests"] == 64
    assert summary["normal_control_degradations"] == ["v3-chinese-paraphrase"]
    assert summary["production_integration_authorized"] is False
    assert summary["numeric_gates_pass"] is False
    assert summary["preregistered_gate_results"]["known_four_exact"] is False
    assert summary["preregistered_gate_results"]["qualified_human_semantic_review"] is False
    assert all(
        r["human_review_status"] == "unreviewed"
        for group in checklist["groups"].values()
        for r in group
    )
    groups = summary["groups"]
    assert groups["regression_b_v2"]["all_raw_quotes"]["numerator"] == 35
    assert groups["regression_b_v2"]["all_raw_quotes"]["denominator"] == 36
    assert groups["regression_b_v2"]["all_raw_quotes"]["failures"] == [
        {"case_id": "v3-quote-short-duplicate", "reason_code": "quote_ambiguous"}
    ]
    assert groups["holdout_a_v1"]["total_score"]["exact"] == {"numerator": 20, "denominator": 20}
    assert groups["holdout_b_v2"]["total_score"]["exact"] == {"numerator": 19, "denominator": 20}
    assert groups["holdout_b_v2"]["fully_correct_not_full_score_cases"] == ["h4-osmosis-paraphrase"]
    assert groups["holdout_b_v2"]["uncertain_probe_abstentions"] == {
        "numerator": 2,
        "denominator": 2,
    }


def test_summary_is_reproducible_without_changing_inputs_predictions_or_labels():
    run = DATA / "rubric-semantics-2026-10-14-run-01"
    files = [
        DATA / "rubric-cases-v4-semantics.json",
        DATA / "rubric-cases-v3.json",
        run / "holdout-v2/quote_selection-predictions.json",
        run / "regression-v2/quote_selection-predictions.json",
    ]
    before = {p: p.read_bytes() for p in files}
    summary, _ = build_summary()
    assert before == {p: p.read_bytes() for p in files}
    assert json.loads((run / "semantics-summary.json").read_bytes()) == summary


def test_frozen_run_integrity_manifest_binds_all_original_model_outputs():
    import hashlib

    run = DATA / "rubric-semantics-2026-10-14-run-01"
    path = run / "run-integrity.json"
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest()
        == path.with_suffix(".json.sha256").read_text().split()[0]
    )
    integrity = json.loads(path.read_bytes())
    assert integrity["new_requests"] == 64 and integrity["automatic_retries"] == 0
    for name, item in integrity["artifacts"].items():
        payload = (run / name).read_bytes()
        assert len(payload) == item["size_bytes"]
        assert hashlib.sha256(payload).hexdigest() == item["sha256"]
