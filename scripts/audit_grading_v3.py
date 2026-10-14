#!/usr/bin/env python3
"""Audit the frozen synthetic v3 grading run without issuing model requests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from huipi_cloud.modules.grading.evaluation import build_grade_input, load_dataset  # noqa: E402
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError  # noqa: E402
from huipi_cloud.modules.grading.quote_evaluation import (  # noqa: E402
    ExperimentPredictionCase,
    QuoteExperimentPredictions,
)
from huipi_cloud.modules.grading.quote_selection import (  # noqa: E402
    GradeQuoteSelectionReply,
    QuoteSelectionReference,
    locate_quote,
)
from scripts.summarize_grading_v3 import _derive_selection_prediction  # noqa: E402
from scripts.validate_grading_v3_holdout import validate_holdout  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data/evaluation/grading"
DEFAULT_RUN = DATA_DIR / "deepseek-flash-grading-v3-2026-10-14-run-01"
EXPECTED_SHA256 = {
    "dataset": "de59c65d31669043ffd49c062318861a542bd039cfd04c7df4d4645dc4df620d",
    "manifest": "4161ea5bfdc74abfa27f727852d96329fdd105c3ccd2aec3166ff7a3d7f8bb08",
    "criteria": "8d4de2bdf2691ad2e494e343af22702665f2aface31eb521e4492700b3a5c861",
    "predictions": "9b141f5af5a962ec988476ab4722c14319b48264292a037dba9a0d163b24eedf",
    "raw_report": "d35963b9c5a2fc5be96b07ae6d91c80e745570f7389a05d5bc7d22601936ad46",
    "historical_summary": "2adeef06f62f793ed6f7124018b75077bb7d004b4f00bd7dc07683b0c9b2e90d",
    "historical_checklist": "8d76aaed728231a2191ec83d62f267ec8580174c1387cdbabc4f9062f1449afd",
}
DISPUTED_CASES = {"v3-stem-copy-number", "v3-econ-opportunity-cost"}

REGRESSION_SPECS: dict[str, dict[str, Any]] = {
    "v3-linear-arithmetic-slip": {
        "criterion_keys": ["division"],
        "categories": ["incorrect_arithmetic_credit", "incorrect_method_credit"],
        "finding": (
            "模型给21÷7的Criterion 3/3；其理由承认学生结果x=3.5错误，"
            "但仍以执行过除法为由给满分。"
        ),
        "disposition": "针对Rubric明确要求正确计算的正向回归案例。",
    },
    "v3-mixed-right-wrong": {
        "criterion_keys": ["division"],
        "categories": ["incorrect_arithmetic_credit", "incorrect_method_credit"],
        "finding": (
            "模型给除以5的Criterion 3/3；学生写x=6，Rubric要求x=5，"
            "模型理由仍把错误结果当成完成步骤。"
        ),
        "disposition": "针对Rubric明确要求正确结果的正向回归案例。",
    },
    "v3-partial-quarter": {
        "criterion_keys": ["conversion"],
        "categories": ["missing_conversion_credit"],
        "finding": "学生仅写75000 cm；模型给750 m换算Criterion 5/5，理由把应当换算误作已经换算。",
        "disposition": "针对不能推断未出现单位换算步骤的正向回归案例。",
    },
    "v3-english-keyword": {
        "criterion_keys": ["product"],
        "categories": ["keyword_only_credit"],
        "finding": "学生只列出oxygen等关键词；模型给4/4，并把词语出现解释为氧气已被说明为产物。",
        "disposition": "Quote字面定位成功，但短关键词不足以证明Criterion语义关系。",
    },
    "v3-stem-copy-number": {
        "criterion_keys": ["formula"],
        "categories": ["source_incomplete", "incorrect_method_credit"],
        "finding": (
            "题干没有给出距离与时间，学生写通用关系式。无法区分学生未代入和题目未提供数据；"
            "模型仍给6/6。"
        ),
        "disposition": "从事后评分敏感性分析排除；修正题目需创建新数据集版本并重新请求模型。",
    },
    "v3-econ-opportunity-cost": {
        "criterion_keys": ["definition", "application"],
        "categories": ["rubric_interpretation_disagreement", "reference_label_dispute"],
        "finding": (
            "合成参考为10分，模型为7分。题干语境可支持参考解释，"
            "但Rubric对最佳替代价值及培训/兼职应用的拆分不够明确。"
        ),
        "disposition": "不作为已确认模型错误；待合格教师仲裁，事后敏感性分析排除。",
    },
    "v3-uncertain-missing-image": {
        "criterion_keys": ["diagram"],
        "categories": ["source_incomplete", "missing_source_returned_zero"],
        "finding": (
            "题目依赖的图像不可用；模型仍产生0分候选并称学生未完成，"
            "把来源缺失混同于学生答错或未作答。"
        ),
        "disposition": (
            "应由评分前Source Readiness门禁阻止模型评分或转人工，"
            "不应把该结果计作学生0分。"
        ),
    },
    "v3-quote-short-duplicate": {
        "criterion_keys": [],
        "categories": ["duplicate_quote_location"],
        "finding": "模型选择的短Quote在学生片段中重复，唯一定位校验以quote_ambiguous拒绝候选。",
        "disposition": "定位/候选覆盖失败，不是评分语义通过或失败样本。",
    },
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"expected_json_object:{path.name}")
    return value


def _write_new_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _verify_source_integrity(dataset_path: Path, run_dir: Path) -> dict[str, str]:
    manifest_path = DATA_DIR / "rubric-cases-v3-manifest.json"
    criteria_path = DATA_DIR / "p3d2a-acceptance-criteria-v1.json"
    paths = {
        "dataset": dataset_path,
        "manifest": manifest_path,
        "criteria": criteria_path,
        "predictions": run_dir / "quote_selection-predictions.json",
        "raw_report": run_dir / "quote_selection-report.json",
        "historical_summary": run_dir / "quality-summary.json",
        "historical_checklist": run_dir / "human-semantic-review.json",
    }
    actual = {key: _sha(path) for key, path in paths.items()}
    for key, expected in EXPECTED_SHA256.items():
        if actual[key] != expected:
            raise ValueError(f"frozen_artifact_sha_mismatch:{key}")

    integrity_path = run_dir / "run-integrity.json"
    integrity = _json(integrity_path)
    integrity_sidecar = integrity_path.with_suffix(".json.sha256")
    sidecar_digest = integrity_sidecar.read_text(encoding="ascii").strip().split()[0]
    if sidecar_digest != _sha(integrity_path):
        raise ValueError("run_integrity_sidecar_mismatch")
    artifact_hashes = integrity.get("artifacts", {})
    for key, file_path in paths.items():
        run_key = {
            "predictions": "quote_selection-predictions.json",
            "raw_report": "quote_selection-report.json",
            "historical_summary": "quality-summary.json",
            "historical_checklist": "human-semantic-review.json",
        }.get(key)
        if run_key and artifact_hashes.get(run_key, {}).get("sha256") != actual[key]:
            raise ValueError(f"run_integrity_artifact_mismatch:{key}")
    if integrity.get("dataset_sha256") != actual["dataset"]:
        raise ValueError("run_integrity_dataset_mismatch")
    if integrity.get("acceptance_criteria_sha256") != actual["criteria"]:
        raise ValueError("run_integrity_criteria_mismatch")
    return actual


def _case_audit(case: Any, manifest_entry: dict[str, Any]) -> dict[str, Any]:
    case_id = case.case_id
    base = {
        "case_id": case_id,
        "reference_status": case.reference_status,
        "label_provenance": case.label_provenance,
        "risk_categories": manifest_entry["risk_categories"],
        "human_review_status": "unreviewed",
        "review_scope": "逐案技术一致性筛查；不等于合格教师审核或教师双评。",
        "included_in_posthoc_sensitivity": (
            case.reference_status == "included" and case_id not in DISPUTED_CASES
        ),
    }
    if case_id == "v3-stem-copy-number":
        if re.search(r"\d", case.stem):
            raise ValueError("stem_copy_case_changed_unexpectedly")
        return {
            **base,
            "audit_classification": "source_incomplete",
            "technical_conclusion": (
                "题干要求求平均速度，但题干没有距离或时间数值；Rubric要求代入数据和数值答案。"
                "参考0分不能区分学生没有代入与题目未提供数据，因此该案例不适合作为完整材料的普通评分样本。"
            ),
            "resolution": (
                "保留v3原样并从事后敏感性分析排除；若修正题目，"
                "须建立新版本并重新请求模型。"
            ),
        }
    if case_id == "v3-econ-opportunity-cost":
        return {
            **base,
            "audit_classification": "reference_label_dispute",
            "technical_conclusion": (
                "参考10分有合理依据：题干给出培训与兼职收入冲突，学生指出放弃兼职工资。"
                "但Rubric分别要求最佳替代选择价值和对培训/兼职选择的应用，学生短句与两项边界的覆盖程度有争议。"
                "未经教师仲裁，不认定参考10或模型7为权威答案。"
            ),
            "resolution": "保留历史标签并从事后敏感性分析排除；human_review_status保持unreviewed。",
        }
    if case.reference_status == "excluded_uncertain":
        return {
            **base,
            "audit_classification": "excluded_uncertain_material_or_rubric_issue",
            "technical_conclusion": case.reference_exclusion_reason,
            "resolution": "保留无分数弃权探针；不强制给0分，也不纳入评分敏感性分析。",
        }
    return {
        **base,
        "audit_classification": "no_specific_issue_found_in_technical_screen",
        "technical_conclusion": (
            "对题干、AnswerKey、Rubric、参考逐项分数、理由及引用作了技术一致性筛查，"
            "本轮未识别到具体材料缺失或明确Rubric冲突；这不是教学标签正确性的证明。"
        ),
        "resolution": "保留冻结v3记录；未进行合格教师复核，需避免将其描述为教师验证标签。",
    }


def _accepted_reply(case: Any, prediction: Any, config: Any):
    status, reply, error = _derive_selection_prediction(case, prediction, config)
    if status != "candidate" or reply is None or reply.outcome != "candidate":
        return None, status, error
    return reply, status, error


def _sensitivity(dataset: Any, predictions: Any) -> dict[str, Any]:
    config = predictions.generation_options.config(predictions.timeout_seconds)
    prediction_by_id = {item.case_id: item for item in predictions.cases}
    eligible_cases = [
        case
        for case in dataset.cases
        if case.reference_status == "included" and case.case_id not in DISPUTED_CASES
    ]
    accepted: list[dict[str, Any]] = []
    for case in eligible_cases:
        reply, _, _ = _accepted_reply(case, prediction_by_id[case.case_id], config)
        if reply is None:
            continue
        doc = build_grade_input(case)
        key_by_id = {
            str(item.criterion_id): case.rubric[index].criterion_key
            for index, item in enumerate(doc.rubric_snapshot)
        }
        predicted = {
            key_by_id[str(item.criterion_id)]: item.awarded_points
            for item in reply.criterion_scores
            if str(item.criterion_id) in key_by_id
        }
        reference = {
            item.criterion_key: item.awarded_points
            for item in case.reference_criterion_scores
        }
        if set(predicted) != set(reference):
            raise ValueError(f"sensitivity_criterion_mismatch:{case.case_id}")
        accepted.append(
            {
                "case_id": case.case_id,
                "total_error": reply.total_score - case.reference_total_score,
                "criterion_errors": {
                    key: predicted[key] - reference[key] for key in reference
                },
            }
        )

    total_errors = [item["total_error"] for item in accepted]
    criterion_errors = [error for item in accepted for error in item["criterion_errors"].values()]
    abs_total = sum((abs(value) for value in total_errors), Decimal("0"))
    abs_criterion = sum((abs(value) for value in criterion_errors), Decimal("0"))
    high_risk = [
        case
        for case in eligible_cases
        if case.reference_total_score == Decimal("0")
    ]
    accepted_by_id = {item["case_id"]: item for item in accepted}
    false_positive_cases = [
        case.case_id
        for case in high_risk
        if case.case_id in accepted_by_id
        and accepted_by_id[case.case_id]["total_error"] > 0
    ]
    return {
        "analysis_type": "post_hoc_sensitivity_analysis",
        "model_calls": 0,
        "uses_original_v3_prompts_and_predictions": True,
        "excluded_disputed_cases": sorted(DISPUTED_CASES),
        "eligible_labeled_cases": len(eligible_cases),
        "accepted_candidate_cases": len(accepted),
        "total_score": {
            "exact_match": {
                "numerator": sum(error == 0 for error in total_errors),
                "denominator": len(total_errors),
            },
            "absolute_error_sum": str(abs_total),
            "mae": str(abs_total / len(total_errors)) if total_errors else None,
            "overestimate_cases": sum(error > 0 for error in total_errors),
            "underestimate_cases": sum(error < 0 for error in total_errors),
        },
        "criterion_score": {
            "exact_match": {
                "numerator": sum(error == 0 for error in criterion_errors),
                "denominator": len(criterion_errors),
            },
            "absolute_error_sum": str(abs_criterion),
            "mae": (
                str(abs_criterion / len(criterion_errors)) if criterion_errors else None
            ),
            "overestimate_criteria": sum(error > 0 for error in criterion_errors),
            "underestimate_criteria": sum(error < 0 for error in criterion_errors),
        },
        "high_risk_zero_reference": {
            "case_count": len(high_risk),
            "accepted_positive_score_case_ids": false_positive_cases,
        },
        "interpretation": (
            "事後敏感性分析，不改寫預註冊結果；沿用原題目與原預測，僅排除兩條有爭議的參考標籤。"
            "不能替代教師審核，也不是新模型基線。"
        ),
    }


def _regression_record(
    case: Any, prediction: Any, spec: dict[str, Any], config: Any
) -> dict[str, Any]:
    reply, status, error = _accepted_reply(case, prediction, config)
    doc = build_grade_input(case)
    key_by_id = {
        str(item.criterion_id): case.rubric[index].criterion_key
        for index, item in enumerate(doc.rubric_snapshot)
    }
    model_scores = {}
    if reply is not None:
        model_scores = {
            key_by_id[str(item.criterion_id)]: item
            for item in reply.criterion_scores
            if str(item.criterion_id) in key_by_id
        }
    ref_scores = {item.criterion_key: item for item in case.reference_criterion_scores}
    criteria = []
    for key in spec["criterion_keys"]:
        reference = ref_scores.get(key)
        predicted = model_scores.get(key)
        rubric = next((item for item in case.rubric if item.criterion_key == key), None)
        criteria.append(
            {
                "criterion_key": key,
                "rubric_requirement": rubric.description if rubric else None,
                "reference_points": str(reference.awarded_points) if reference else None,
                "reference_rationale": reference.rationale if reference else None,
                "reference_evidence": (
                    [
                        {
                            "segment_index": item.segment_index,
                            "text_start": item.text_start,
                            "quote": item.quote,
                        }
                        for item in reference.evidence
                    ]
                    if reference
                    else []
                ),
                "model_points": str(predicted.awarded_points) if predicted else None,
                "model_rationale": predicted.rationale if predicted else None,
                "model_evidence": [
                    {
                        "kind": (
                            "quote"
                            if getattr(item, "kind", None) == "quote"
                            else "no_evidence"
                        ),
                        "quote": item.quote if getattr(item, "kind", None) == "quote" else None,
                        "segment_index": (
                            item.segment_index if getattr(item, "kind", None) == "quote" else None
                        ),
                        "source_segment_text": (
                            case.student_segments[item.segment_index].text
                            if getattr(item, "kind", None) == "quote"
                            else None
                        ),
                        "reason": (
                            item.reason
                            if getattr(item, "kind", None) == "no_evidence"
                            else None
                        ),
                    }
                    for item in (predicted.evidence_references if predicted else [])
                ],
            }
        )
    raw_quote_references = []
    if prediction.response_content:
        try:
            raw_reply = GradeQuoteSelectionReply.model_validate_json(
                prediction.response_content
            )
            for score in raw_reply.criterion_scores:
                criterion_key = key_by_id.get(str(score.criterion_id))
                for evidence in score.evidence_references:
                    if not isinstance(evidence, QuoteSelectionReference):
                        continue
                    location_status = "unique_exact"
                    try:
                        locate_quote(doc, evidence.segment_index, evidence.quote, config)
                    except GradeProposalError as location_error:
                        location_status = location_error.reason_code
                    raw_quote_references.append(
                        {
                            "criterion_key": criterion_key,
                            "segment_index": evidence.segment_index,
                            "quote": evidence.quote,
                            "source_segment_text": case.student_segments[
                                evidence.segment_index
                            ].text,
                            "location_status": location_status,
                        }
                    )
        except (ValueError, TypeError, KeyError):
            raw_quote_references = []
    return {
        "case_id": case.case_id,
        "question": case.stem,
        "answer_key": case.answer_key,
        "rubric": [item.model_dump(mode="json") for item in case.rubric],
        "student_segments": [item.model_dump(mode="json") for item in case.student_segments],
        "reference_total_points": (
            str(case.reference_total_score) if case.reference_total_score is not None else None
        ),
        "model_total_points": str(reply.total_score) if reply is not None else None,
        "prediction_status": status,
        "prediction_error_code": error,
        "reference_label_status": (
            "disputed_unreviewed"
            if case.case_id in DISPUTED_CASES
            else "synthetic_curated_unreviewed"
        ),
        "categories": spec["categories"],
        "technical_finding": spec["finding"],
        "regression_expectation": spec["disposition"],
        "criteria": criteria,
        "raw_quote_references": raw_quote_references,
        "human_review_status": "unreviewed",
    }


def build_audit_artifacts(
    dataset_path: Path = DATA_DIR / "rubric-cases-v3.json",
    run_dir: Path = DEFAULT_RUN,
) -> tuple[dict[str, Any], dict[str, Any]]:
    hashes = _verify_source_integrity(dataset_path, run_dir)
    manifest_path = DATA_DIR / "rubric-cases-v3-manifest.json"
    criteria_path = DATA_DIR / "p3d2a-acceptance-criteria-v1.json"
    validation = validate_holdout(dataset_path, manifest_path, criteria_path)
    dataset, dataset_sha = load_dataset(dataset_path)
    predictions = QuoteExperimentPredictions.model_validate_json(
        (run_dir / "quote_selection-predictions.json").read_bytes()
    )
    if predictions.dataset_sha256 != dataset_sha:
        raise ValueError("prediction_dataset_digest_mismatch")
    config = predictions.generation_options.config(predictions.timeout_seconds)
    manifest = _json(manifest_path)
    manifest_by_id = {item["case_id"]: item for item in manifest["cases"]}
    predictions_by_id = {item.case_id: item for item in predictions.cases}
    if set(predictions_by_id) != {case.case_id for case in dataset.cases}:
        raise ValueError("prediction_case_set_mismatch")
    probe_doc = _json(run_dir / "abstention-probes.json")
    probe_by_id = {
        item.case_id: item
        for item in (
            ExperimentPredictionCase.model_validate(record)
            for record in probe_doc.get("cases", [])
        )
    }

    case_audits = [
        _case_audit(case, manifest_by_id[case.case_id]) for case in dataset.cases
    ]
    classes = Counter(item["audit_classification"] for item in case_audits)
    sensitivity = _sensitivity(dataset, predictions)
    regression_records = [
        _regression_record(
            next(case for case in dataset.cases if case.case_id == case_id),
            probe_by_id.get(case_id, predictions_by_id[case_id]),
            spec,
            config,
        )
        for case_id, spec in REGRESSION_SPECS.items()
    ]
    audit_report = {
        "schema_name": "huipi.grading.v3-reference-label-audit",
        "schema_version": "1.0",
        "audit_type": "post_hoc_technical_audit",
        "source_dataset_version": dataset.dataset_version,
        "source_dataset_sha256": dataset_sha,
        "source_manifest_sha256": hashes["manifest"],
        "source_preregistered_criteria_sha256": hashes["criteria"],
        "source_prediction_sha256": hashes["predictions"],
        "source_original_report_sha256": hashes["raw_report"],
        "source_historical_summary_sha256": hashes["historical_summary"],
        "source_historical_checklist_sha256": hashes["historical_checklist"],
        "case_count": len(case_audits),
        "case_classification_counts": dict(sorted(classes.items())),
        "human_review_status": "unreviewed",
        "teacher_double_scored": False,
        "model_predictions_used_to_change_labels": False,
        "original_v3_inputs_labels_predictions_and_preregistration_modified": False,
        "case_audits": case_audits,
        "posthoc_sensitivity_analysis": sensitivity,
        "source_validation": validation,
        "limitations": [
            "本报告是逐案技术筛查，不是合格教师复核或双评。",
            "事后敏感性分析沿用原v3模型预测，只排除题目材料缺失与标签争议两例；不参与v1预注册门槛判定。",
            "未对修改后的题目发起模型请求，也没有创建修改版数据集。",
        ],
    }
    regression_report = {
        "schema_name": "huipi.grading.p3d2a-error-regression-analysis",
        "schema_version": "1.0",
        "analysis_version": "v1",
        "source_dataset_version": dataset.dataset_version,
        "source_dataset_sha256": dataset_sha,
        "source_prediction_sha256": hashes["predictions"],
        "model_id": predictions.model_id,
        "model_requests_made": 0,
        "human_review_status": "unreviewed",
        "categories_allow_overlap": True,
        "records": regression_records,
        "notes": [
            "模型行为问题、来源材料问题和参考标签争议分开标记。",
            "来源字面定位成功不表示语义支持；模型错误分类不能替代人工教学判断。",
            "v3-stem-copy-number和v3-econ-opportunity-cost不进入事后分数敏感性分母。",
        ],
    }
    return audit_report, regression_report


def write_audit_artifacts(
    audit_report: dict[str, Any], regression_report: dict[str, Any]
) -> tuple[Path, Path]:
    audit_path = DATA_DIR / "rubric-cases-v3-audit-v1.json"
    regression_path = DATA_DIR / "p3d2a-error-regression-v1.json"
    paths = (
        audit_path,
        audit_path.with_suffix(audit_path.suffix + ".sha256"),
        regression_path,
        regression_path.with_suffix(regression_path.suffix + ".sha256"),
    )
    if any(path.exists() for path in paths):
        raise FileExistsError("audit_artifact_already_exists")
    _write_new_json(audit_path, audit_report)
    _write_new_json(regression_path, regression_report)
    audit_sha = _sha(audit_path)
    regression_sha = _sha(regression_path)
    with audit_path.with_suffix(audit_path.suffix + ".sha256").open(
        "x", encoding="ascii"
    ) as stream:
        stream.write(f"{audit_sha}  {audit_path.name}\n")
    with regression_path.with_suffix(regression_path.suffix + ".sha256").open(
        "x", encoding="ascii"
    ) as stream:
        stream.write(f"{regression_sha}  {regression_path.name}\n")
    return audit_path, regression_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--dataset", type=Path, default=DATA_DIR / "rubric-cases-v3.json")
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    args = parser.parse_args()
    try:
        audit, regression = build_audit_artifacts(args.dataset, args.run_dir)
        audit_path, regression_path = write_audit_artifacts(audit, regression)
        print(
            json.dumps(
                {
                    "audit_report": str(audit_path),
                    "regression_list": str(regression_path),
                    "dataset_sha256": audit["source_dataset_sha256"],
                    "case_count": audit["case_count"],
                    "sensitivity": audit["posthoc_sensitivity_analysis"],
                    "model_requests": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"v3评测审计失败：{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
