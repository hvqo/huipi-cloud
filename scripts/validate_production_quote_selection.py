"""Bounded opt-in synthetic acceptance. Does not write to the frozen baseline."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import subprocess
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider
from huipi_cloud.modules.grading.evaluation import build_grade_input, load_dataset
from huipi_cloud.modules.grading.prompts_selection import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError
from huipi_cloud.modules.grading.proposal_protocol import QuoteEvidenceReference
from huipi_cloud.modules.grading.proposal_service import (
    _call_and_validate,
    _validate_quote_reference,
)
from huipi_cloud.modules.grading.quote_selection import GradeQuoteSelectionReply, locate_quote

DATA = Path("data/evaluation/grading/deepseek-prompt-v4-smoke-v1.json")
DATA_SHA = "43215a6deae4bc8f144dbeb9e5b65dfc9186dda76274860e489192c748d6d69b"
BASELINE = Path("data/evaluation/grading/deepseek-prompt-v4-smoke-predictions-v1.json")


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
    path.chmod(0o600)


def reserve_call(directory: Path):
    """Shared with the one real business E2E. No retries or parallel budget races."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "call-counter.json").open("a+") as f:
        os.chmod(f.name, 0o600)
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        content = f.read()
        state = json.loads(content) if content else {"attempted": 0, "max": 13}
        # Read again from the beginning when the file was nonempty.
        if state.get("attempted") is None:
            raise RuntimeError("invalid_call_counter")
        if state["attempted"] >= 13:
            raise RuntimeError("round_request_limit")
        state["attempted"] += 1
        f.seek(0)
        f.truncate()
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())


def live_config():
    cfg = Settings()
    if not (
        cfg.grading_allow_remote and cfg.grading_external_data_authorized and cfg.grading_api_key
    ):
        raise RuntimeError("explicit_remote_authorization_and_key_required")
    if (
        cfg.grading_base_url.rstrip("/") != "https://api.deepseek.com"
        or cfg.grading_model != "deepseek-flash"
        or cfg.grading_provider != "openai_compatible"
    ):
        raise RuntimeError("only_authorized_deepseek_flash")
    return cfg.model_copy(
        update={
            "grading_enabled": True,
            "grading_strategy": "quote_selection",
            "grading_deepseek_thinking": "disabled",
            "grading_timeout_seconds": 120,
            "grading_max_output_tokens": 4096,
            "grading_invalid_response_retries": 0,
        }
    )


def offline_feasibility(dataset, baseline):
    cases = {c.case_id: c for c in dataset.cases}
    refs = []
    for case in baseline["cases"]:
        if case["status"] in {"candidate", "abstain"}:
            continue
        doc = build_grade_input(cases[case["case_id"]])
        raw = json.loads(case["response_content"])
        for score in raw.get("criterion_scores", []):
            for ref in score.get("evidence_references", []):
                if ref.get("kind") != "quote":
                    continue
                try:
                    _validate_quote_reference(
                        QuoteEvidenceReference.model_validate(ref), doc, Settings()
                    )
                    original_error = None
                except (GradeProposalError, ValidationError) as error:
                    original_error = getattr(error, "reason_code", "quote_schema_invalid")
                try:
                    found = locate_quote(doc, ref["segment_index"], ref["quote"], Settings())
                    status = "unique_exact"
                    bounds = [found.text_start, found.text_end]
                except GradeProposalError as error:
                    status, bounds = error.reason_code, None
                refs.append(
                    dict(
                        case_id=case["case_id"],
                        status=status,
                        located_range=bounds,
                        claimed_range=[ref["text_start"], ref["text_end"]],
                        original_quote_error=original_error,
                    )
                )
    return {"diagnostic_only": True, "historical_statuses_unchanged": True, "references": refs}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    dataset, digest = load_dataset(DATA)
    if digest != DATA_SHA:
        raise RuntimeError("frozen_dataset_sha_mismatch")
    old_bytes = BASELINE.read_bytes()
    baseline = json.loads(old_bytes)
    baseline_sha = hashlib.sha256(old_bytes).hexdigest()
    expected_sha = Path(str(BASELINE) + ".sha256").read_text().split()[0]
    if baseline_sha != expected_sha:
        raise RuntimeError("frozen_prediction_sha_mismatch")
    cfg = live_config()
    provider = build_text_grading_provider(cfg)
    old_inputs = {row["case_id"]: row["input_sha256"] for row in baseline["preflight"]["inputs"]}
    if old_inputs != {c.case_id: build_grade_input(c).input_sha256 for c in dataset.cases}:
        raise RuntimeError("paired_inputs_changed")
    # Conservative UTF-8-byte token bound; reserve a separate bounded E2E prompt.
    inputs = sum(
        len(SYSTEM_PROMPT.encode()) + len(build_user_prompt(build_grade_input(c)).encode()) + 1024
        for c in dataset.cases
    )
    bound = (
        Decimal(inputs + 20000) * Decimal(".30") + Decimal(13 * 4096) * Decimal("1.20")
    ) / 1000000
    if bound > Decimal(".10"):
        raise RuntimeError("round_budget_exceeded_before_request")
    manifest = dict(
        dataset_sha256=digest,
        baseline_sha256=baseline_sha,
        prompt_version=PROMPT_VERSION,
        model=provider.model_id,
        thinking="disabled",
        max_tokens=4096,
        timeout_seconds=120,
        retries=0,
        max_round_requests=13,
        budget_usd=".10",
        peak_cost_bound_usd=str(bound),
        generation_config_digest=provider.generation_config_digest,
        frozen_head=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        synthetic_only=True,
        reference_kind="synthetic_curated_reference",
        human_review_status="unreviewed",
        created_at=datetime.now(UTC).isoformat(),
        inputs=[
            dict(case_id=c.case_id, input_sha256=build_grade_input(c).input_sha256)
            for c in dataset.cases
        ],
    )
    if (args.output / "started").exists():
        raise RuntimeError("never_overwrite_or_repeat_round")
    save(args.output / "preflight.json", manifest)
    save(args.output / "offline-feasibility.json", offline_feasibility(dataset, baseline))
    print(json.dumps({"phase": "preflight", "cost_bound_usd": str(bound)}), flush=True)
    if not args.run:
        return
    fd = os.open(args.output / "started", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    rows = []
    original = provider.complete
    capture = {}

    async def complete(**kwargs):
        reserve_call(args.output)
        response = await original(**kwargs)
        capture["response"] = response
        return response

    provider.complete = complete
    for case in dataset.cases:
        capture.clear()
        doc = build_grade_input(case)
        row = dict(
            case_id=case.case_id,
            input_sha256=doc.input_sha256,
            status="provider_error",
            converted_reply=None,
            human_review_status="unreviewed",
        )
        try:
            reply, _, _, _ = await _call_and_validate(provider, doc, config=cfg)
            row.update(status=reply.outcome, converted_reply=reply.model_dump(mode="json"))
        except GradeProposalError as error:
            row.update(status=error.status, error_code=error.reason_code)
        response = capture.get("response")
        row["response"] = asdict(response) if response else None
        row["json_valid"], row["schema_valid"] = False, False
        row["raw_quote_count"], row["located_quote_count"] = 0, 0
        row["quote_failures"] = []
        if response:
            try:
                raw = json.loads(response.content)
                row["json_valid"] = True
                GradeQuoteSelectionReply.model_validate(raw)
                row["schema_valid"] = True
            except (ValueError, ValidationError):
                raw = None if not row["json_valid"] else raw
            if isinstance(raw, dict):
                for score in raw.get("criterion_scores", []):
                    for ref in score.get("evidence_references", []):
                        if ref.get("kind") == "quote_selection":
                            row["raw_quote_count"] += 1
                            try:
                                locate_quote(doc, ref["segment_index"], ref["quote"], cfg)
                                row["located_quote_count"] += 1
                            except (GradeProposalError, KeyError) as error:
                                row["quote_failures"].append(getattr(error, "reason_code", "shape"))
        if row["status"] == "candidate" and case.reference_total_score is not None:
            row["total_delta"] = str(reply.total_score - case.reference_total_score)
            expected = {r.criterion_key: r.awarded_points for r in case.reference_criterion_scores}
            actual = {r.criterion_id: r.awarded_points for r in reply.criterion_scores}
            row["criterion_deltas"] = {
                rule.criterion_key: str(
                    actual[criterion.criterion_id] - expected[rule.criterion_key]
                )
                for rule, criterion in zip(case.rubric, doc.rubric_snapshot, strict=True)
            }
        rows.append(row)
        save(args.output / "predictions.json", {"preflight": manifest, "cases": rows})
        print(
            json.dumps(
                {
                    "case_id": case.case_id,
                    "status": row["status"],
                    "error_code": row.get("error_code"),
                }
            ),
            flush=True,
        )
    summary = dict(
        statuses=dict(Counter(r["status"] for r in rows)),
        json_valid=sum(r["json_valid"] for r in rows),
        schema_valid=sum(r["schema_valid"] for r in rows),
        raw_quotes=sum(r["raw_quote_count"] for r in rows),
        located_quotes=sum(r["located_quote_count"] for r in rows),
        total_exact=sum(
            r.get("total_delta") is not None and Decimal(r["total_delta"]) == 0 for r in rows
        ),
        all_semantic_reviews="unreviewed",
        historical_statuses_unchanged=True,
    )
    save(args.output / "summary.json", summary)
    assert hashlib.sha256(DATA.read_bytes()).hexdigest() == digest
    assert hashlib.sha256(BASELINE.read_bytes()).hexdigest() == baseline_sha


if __name__ == "__main__":
    asyncio.run(main())
