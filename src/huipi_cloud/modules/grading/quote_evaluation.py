"""File-only paired experiment using the existing provider and score metrics."""

from __future__ import annotations

import hashlib
import math
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import (
    GradeProviderEndedResponseError,
    TextGradingProvider,
)
from huipi_cloud.infrastructure.llm.deepseek import finish_reason_error, is_deepseek_endpoint
from huipi_cloud.modules.grading.evaluation import (
    EvaluationPredictionCase,
    RubricEvaluationCase,
    RubricEvaluationDataset,
    RubricEvaluationPredictions,
    _candidate_quote_sources_valid,
    _derive_prediction,
    _rubric_score_constraints_valid,
    _status_for_error,
    _status_for_model_call_error,
    build_grade_input,
    evaluate_predictions,
)
from huipi_cloud.modules.grading.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError
from huipi_cloud.modules.grading.proposal_protocol import GradeModelReply
from huipi_cloud.modules.grading.proposal_service import (
    _check_prompt_budget,
    _classify_reply_validation_error,
    _validate_candidate_scores,
)
from huipi_cloud.modules.grading.quote_selection import (
    QUOTE_SELECTION_PROMPT_VERSION,
    GradeQuoteSelectionReply,
    QuoteSelectionReference,
    convert_quote_selections,
    locate_quote,
    quote_selection_prompts,
)

ExperimentMode = Literal["strict", "quote_selection"]


class ExperimentOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature: Literal[0] = 0
    max_output_tokens: int = Field(ge=64, le=4096)
    max_input_bytes: int = Field(ge=1024, le=256 * 1024)
    max_input_tokens: int = Field(ge=1024, le=256 * 1024)
    max_rationale_characters: int = Field(ge=100, le=4000)
    max_quote_characters: int = Field(ge=20, le=2000)

    @classmethod
    def from_config(cls, config: Settings) -> ExperimentOptions:
        return cls.model_validate(
            {
                name: getattr(config, f"grading_{name}")
                for name in cls.model_fields
                if name != "temperature"
            }
        )

    def config(self, timeout_seconds: float) -> Settings:
        return Settings(
            **{
                f"grading_{name}": value
                for name, value in self.model_dump().items()
                if name != "temperature"
            },
            grading_timeout_seconds=timeout_seconds,
            grading_invalid_response_retries=0,
        )


class ExperimentPredictionCase(EvaluationPredictionCase):
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: UUID | None = None
    response_model_id: str | None = Field(default=None, min_length=1, max_length=200)
    provider_attempted: bool = False


class QuoteExperimentPredictions(RubricEvaluationPredictions):
    experiment_config_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    schema_name: Literal["huipi.grading.quote-experiment.predictions"] = (
        "huipi.grading.quote-experiment.predictions"
    )
    mode: ExperimentMode
    generation_options: ExperimentOptions
    invalid_response_retries: Literal[0] = 0
    cases: list[ExperimentPredictionCase] = Field(min_length=1, max_length=100)


def experiment_prompts(
    case: RubricEvaluationCase, mode: ExperimentMode, prompt_version: str | None = None
) -> tuple[str, str]:
    document = build_grade_input(case)
    if mode == "strict":
        if prompt_version not in {None, PROMPT_VERSION}:
            raise ValueError("experiment_prompt_version_mismatch")
        return SYSTEM_PROMPT, build_user_prompt(document)
    return quote_selection_prompts(document, prompt_version or QUOTE_SELECTION_PROMPT_VERSION)


def _prompt_sha256(
    case: RubricEvaluationCase, mode: ExperimentMode, prompt_version: str | None = None
) -> str:
    system, user = experiment_prompts(case, mode, prompt_version)
    return hashlib.sha256((system + "\0" + user).encode("utf-8")).hexdigest()


def unrequested_prediction(
    case: RubricEvaluationCase, mode: ExperimentMode, prompt_version: str | None = None
) -> ExperimentPredictionCase:
    return ExperimentPredictionCase(
        case_id=case.case_id,
        status="excluded_uncertain" if case.reference_status != "included" else "not_requested",
        input_sha256=build_grade_input(case).input_sha256,
        prompt_sha256=_prompt_sha256(case, mode, prompt_version),
    )


def _validate_selection_response(
    case: RubricEvaluationCase,
    content: str,
    config: Settings,
) -> tuple[str, GradeModelReply | None, str | None]:
    try:
        selected = GradeQuoteSelectionReply.model_validate_json(content)
    except ValidationError as error:
        return "protocol_error", None, _classify_reply_validation_error(error).reason_code
    document = build_grade_input(case)
    try:
        reply = convert_quote_selections(selected, document, config)
    except GradeProposalError as error:
        return _status_for_error(error), None, error.reason_code
    if reply.outcome == "abstain":
        return "abstain", reply, None
    try:
        _validate_candidate_scores(reply, document, config)
    except GradeProposalError as error:
        return _status_for_error(error), reply, error.reason_code
    return "candidate", reply, None


def _derive_selection_prediction(
    case: RubricEvaluationCase,
    prediction: EvaluationPredictionCase,
    config: Settings,
) -> tuple[str, GradeModelReply | None, str | None]:
    ended_reason = finish_reason_error(
        prediction.finish_reason,
        required=prediction.error_code == "provider_finish_reason_missing",
    )
    if ended_reason is not None:
        if prediction.status != "provider_error" or prediction.error_code != ended_reason:
            raise ValueError("experiment_finish_reason_error_mismatch")
        return "provider_error", None, ended_reason
    if prediction.response_content is None:
        return _derive_prediction(case, prediction, config)
    result = _validate_selection_response(case, prediction.response_content, config)
    if (prediction.status, prediction.error_code) != (result[0], result[2]):
        raise ValueError("experiment_status_or_error_does_not_match_raw_reply")
    return result


async def request_experiment_prediction(
    case: RubricEvaluationCase,
    mode: ExperimentMode,
    provider: TextGradingProvider,
    config: Settings,
    *,
    prompt_version: str | None = None,
) -> ExperimentPredictionCase:
    """One provider call, no retries; raw reply is never repaired or persisted as a Proposal."""

    metadata = unrequested_prediction(case, mode, prompt_version).model_dump(
        exclude={"status", "error_code", "response_content"}
    )
    started = time.perf_counter()
    system, user = experiment_prompts(case, mode, prompt_version)
    try:
        _check_prompt_budget(system, user, config)
        metadata["request_id"] = uuid4()
        metadata["provider_attempted"] = True
        response = await provider.complete(system_prompt=system, user_prompt=user)
    except GradeProviderEndedResponseError as error:
        response = error.response
        metadata.update(
            response_model_id=response.model_id,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            elapsed_ms=response.elapsed_ms,
            finish_reason=response.finish_reason,
            reasoning_tokens=response.usage.reasoning_tokens,
            cache_hit_tokens=response.usage.cache_hit_tokens,
            cache_miss_tokens=response.usage.cache_miss_tokens,
        )
        return ExperimentPredictionCase(
            **metadata,
            status="provider_error",
            error_code=error.reason_code,
            response_content=response.content,
        )
    except GradeProposalError as error:
        metadata["elapsed_ms"] = max(0, round((time.perf_counter() - started) * 1000))
        return ExperimentPredictionCase(
            **metadata,
            status=_status_for_model_call_error(error),
            error_code=error.reason_code,
        )
    metadata.update(
        response_model_id=response.model_id,
        prompt_tokens=response.usage.prompt_tokens,
        completion_tokens=response.usage.completion_tokens,
        elapsed_ms=response.elapsed_ms,
        finish_reason=response.finish_reason,
        reasoning_tokens=response.usage.reasoning_tokens,
        cache_hit_tokens=response.usage.cache_hit_tokens,
        cache_miss_tokens=response.usage.cache_miss_tokens,
    )
    if mode == "quote_selection":
        status, _, error_code = _validate_selection_response(case, response.content, config)
    else:
        try:
            reply = GradeModelReply.model_validate_json(response.content)
        except ValidationError as error:
            status, error_code = (
                "protocol_error",
                _classify_reply_validation_error(error).reason_code,
            )
        else:
            status, error_code = reply.outcome, None
            if reply.outcome == "candidate":
                try:
                    _validate_candidate_scores(reply, build_grade_input(case), config)
                except GradeProposalError as error:
                    status, error_code = _status_for_error(error), error.reason_code
    return ExperimentPredictionCase(
        **metadata, status=status, error_code=error_code, response_content=response.content
    )


def create_experiment_predictions(
    dataset: RubricEvaluationDataset,
    digest: str,
    mode: ExperimentMode,
    provider: TextGradingProvider,
    config: Settings,
    cases: list[ExperimentPredictionCase],
    *,
    prompt_version: str | None = None,
) -> QuoteExperimentPredictions:
    version = prompt_version or (
        PROMPT_VERSION if mode == "strict" else QUOTE_SELECTION_PROMPT_VERSION
    )
    experiment_prompts(dataset.cases[0], mode, version)
    return QuoteExperimentPredictions(
        experiment_config_digest=hashlib.sha256(
            (provider.generation_config_digest + "\0" + version).encode()
        ).hexdigest(),
        mode=mode,
        dataset_version=dataset.dataset_version,
        dataset_sha256=digest,
        provider=provider.provider_name,
        model_id=provider.model_id,
        prompt_version=version,
        generation_config_digest=provider.generation_config_digest,
        thinking_mode=(
            config.grading_deepseek_thinking
            if is_deepseek_endpoint(config.grading_base_url)
            else None
        ),
        reasoning_effort=(
            config.grading_deepseek_reasoning_effort
            if is_deepseek_endpoint(config.grading_base_url)
            and config.grading_deepseek_thinking == "enabled"
            else None
        ),
        configured_reasoning_effort=(
            config.grading_deepseek_reasoning_effort
            if is_deepseek_endpoint(config.grading_base_url)
            else None
        ),
        max_output_tokens=config.grading_max_output_tokens,
        generation_options=ExperimentOptions.from_config(config),
        timeout_seconds=config.grading_timeout_seconds,
        created_at=datetime.now(UTC),
        cases=cases,
    )


def import_strict_baseline(
    dataset: RubricEvaluationDataset,
    digest: str,
    strict_predictions: RubricEvaluationPredictions,
    provider: TextGradingProvider,
    config: Settings,
) -> QuoteExperimentPredictions:
    """Wrap the immutable P3-B strict run for comparison without inventing request IDs."""

    if (strict_predictions.dataset_version, strict_predictions.dataset_sha256) != (
        dataset.dataset_version,
        digest,
    ):
        raise ValueError("strict_baseline_dataset_mismatch")
    expected_metadata = {
        "provider": provider.provider_name,
        "model_id": provider.model_id,
        "prompt_version": PROMPT_VERSION,
        "generation_config_digest": provider.generation_config_digest,
        "thinking_mode": config.grading_deepseek_thinking,
        "reasoning_effort": (
            config.grading_deepseek_reasoning_effort
            if config.grading_deepseek_thinking == "enabled"
            else None
        ),
        "configured_reasoning_effort": config.grading_deepseek_reasoning_effort,
        "max_output_tokens": config.grading_max_output_tokens,
        "invalid_response_retries": 0,
        "timeout_seconds": config.grading_timeout_seconds,
    }
    mismatched_fields = [
        key for key, value in expected_metadata.items() if getattr(strict_predictions, key) != value
    ]
    if mismatched_fields:
        raise ValueError(
            "strict_baseline_generation_config_mismatch:" + ",".join(mismatched_fields)
        )
    expected_ids = [case.case_id for case in dataset.cases]
    actual_ids = [case.case_id for case in strict_predictions.cases]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_ids):
        raise ValueError("strict_baseline_case_set_mismatch")

    config_by_case = {case.case_id: case for case in dataset.cases}
    converted_cases = []
    for prediction in strict_predictions.cases:
        case = config_by_case[prediction.case_id]
        converted_cases.append(
            ExperimentPredictionCase(
                **prediction.model_dump(),
                input_sha256=build_grade_input(case).input_sha256,
                prompt_sha256=_prompt_sha256(case, "strict"),
                request_id=None,
                provider_attempted=prediction.status
                not in {"not_requested", "excluded_uncertain", "input_too_large"},
            )
        )
    return QuoteExperimentPredictions(
        mode="strict",
        dataset_version=dataset.dataset_version,
        dataset_sha256=digest,
        provider=provider.provider_name,
        model_id=provider.model_id,
        prompt_version=PROMPT_VERSION,
        generation_config_digest=provider.generation_config_digest,
        thinking_mode=strict_predictions.thinking_mode,
        reasoning_effort=strict_predictions.reasoning_effort,
        configured_reasoning_effort=strict_predictions.configured_reasoning_effort,
        max_output_tokens=strict_predictions.max_output_tokens,
        generation_options=ExperimentOptions.from_config(config),
        timeout_seconds=config.grading_timeout_seconds,
        created_at=strict_predictions.created_at,
        cases=converted_cases,
    )


def _selection_stages(dataset, predictions, config: Settings) -> dict:
    by_id = {case.case_id: case for case in dataset.cases}
    protocol_cases = candidate_cases = rubric_valid = quote_valid = 0
    selected_refs = located_refs = 0
    location_failures = []
    protocol_failures = []
    provider_failures = []
    candidate_gate_failures = []
    for prediction in predictions.cases:
        if prediction.status == "invalid_evidence":
            candidate_gate_failures.append(
                {
                    "case_id": prediction.case_id,
                    "reason_code": prediction.error_code,
                }
            )
        if prediction.response_content is None:
            continue
        case = by_id[prediction.case_id]
        if prediction.status == "provider_error":
            provider_failures.append(
                {
                    "case_id": case.case_id,
                    "reason_code": prediction.error_code,
                    "finish_reason": prediction.finish_reason,
                    "partial_response_preserved": True,
                }
            )
            continue
        try:
            reply = GradeQuoteSelectionReply.model_validate_json(prediction.response_content)
        except ValidationError as error:
            protocol_failures.append(
                {
                    "case_id": case.case_id,
                    "errors": [
                        {"location": list(detail["loc"]), "type": detail["type"]}
                        for detail in error.errors(include_input=False, include_context=False)
                    ],
                }
            )
            continue
        protocol_cases += 1
        if reply.outcome != "candidate":
            continue
        candidate_cases += 1
        rubric_valid += int(_rubric_score_constraints_valid(case, reply))
        document = build_grade_input(case)
        for criterion_index, score in enumerate(reply.criterion_scores):
            for evidence_index, evidence in enumerate(score.evidence_references):
                if not isinstance(evidence, QuoteSelectionReference):
                    continue
                selected_refs += 1
                try:
                    locate_quote(document, evidence.segment_index, evidence.quote, config)
                except GradeProposalError as error:
                    location_failures.append(
                        {
                            "case_id": case.case_id,
                            "criterion_index": criterion_index,
                            "evidence_index": evidence_index,
                            "reason_code": error.reason_code,
                        }
                    )
                else:
                    located_refs += 1
        try:
            converted = convert_quote_selections(reply, document, config)
        except GradeProposalError:
            continue
        quote_valid += int(_candidate_quote_sources_valid(case, converted, config)[0])
    failure_counts = Counter(item["reason_code"] for item in location_failures)
    failure_cases = {
        code: len({item["case_id"] for item in location_failures if item["reason_code"] == code})
        for code in failure_counts
    }
    return {
        "protocol_valid_cases": protocol_cases,
        "candidate_outcome_cases": candidate_cases,
        "rubric_valid_cases": rubric_valid,
        "quote_valid_cases": quote_valid,
        "selection_references": selected_refs,
        "unique_exact_located_references": located_refs,
        "location_failure_occurrences": dict(sorted(failure_counts.items())),
        "location_failure_case_counts": dict(sorted(failure_cases.items())),
        "location_failures": location_failures,
        "protocol_failure_cases": protocol_failures,
        "provider_failure_cases": provider_failures,
        "candidate_gate_failures": candidate_gate_failures,
    }


def evaluate_experiment(
    dataset: RubricEvaluationDataset,
    digest: str,
    predictions: QuoteExperimentPredictions,
) -> dict:
    experiment_prompts(dataset.cases[0], predictions.mode, predictions.prompt_version)
    expected_config_digest = hashlib.sha256(
        (predictions.generation_config_digest + "\0" + predictions.prompt_version).encode()
    ).hexdigest()
    if predictions.experiment_config_digest not in {None, expected_config_digest}:
        raise ValueError("experiment_config_digest_mismatch")
    by_case_id = {case.case_id: case for case in dataset.cases}
    for prediction in predictions.cases:
        case = by_case_id.get(prediction.case_id)
        if case is None:
            raise ValueError("prediction_case_set_mismatch")
        if prediction.input_sha256 != build_grade_input(case).input_sha256:
            raise ValueError("experiment_input_sha256_mismatch")
        if prediction.prompt_sha256 != _prompt_sha256(
            case, predictions.mode, predictions.prompt_version
        ):
            raise ValueError("experiment_prompt_sha256_mismatch")
        if prediction.response_content is not None and (
            not prediction.provider_attempted
            or prediction.response_model_id != predictions.model_id
        ):
            raise ValueError("experiment_response_model_or_request_identity_mismatch")
        if prediction.request_id is not None and not prediction.provider_attempted:
            raise ValueError("request_identity_requires_provider_attempt")
        if prediction.status in {"not_requested", "excluded_uncertain"} and (
            prediction.provider_attempted
        ):
            raise ValueError("unrequested_case_cannot_be_provider_attempted")
        if prediction.status in {"not_requested", "excluded_uncertain"} and (
            prediction.request_id is not None or prediction.response_model_id is not None
        ):
            raise ValueError("unrequested_case_cannot_contain_request_metadata")
    config = predictions.generation_options.config(predictions.timeout_seconds)
    report = evaluate_predictions(
        dataset,
        digest,
        predictions,
        config=config,
        reply_evaluator=(
            _derive_selection_prediction if predictions.mode == "quote_selection" else None
        ),
    )
    report["experiment_mode"] = predictions.mode
    report["generation_options"] = predictions.generation_options.model_dump()
    report["counts"]["provider_requests_sent"] = sum(
        prediction.provider_attempted for prediction in predictions.cases
    )
    durations = sorted(
        prediction.elapsed_ms
        for prediction in predictions.cases
        if prediction.provider_attempted and prediction.elapsed_ms is not None
    )
    report["runtime_usage"].update(
        elapsed_ms_p50=durations[math.ceil(len(durations) * 0.50) - 1] if durations else None,
        elapsed_ms_p95=durations[math.ceil(len(durations) * 0.95) - 1] if durations else None,
        percentile_definition="nearest_rank; actual provider attempts including failures",
    )
    if predictions.mode == "quote_selection":
        stages = _selection_stages(dataset, predictions, config)
        report["quote_location"] = stages
        report["protocol_failure_diagnostics"] = {
            "cases": stages["protocol_failure_cases"],
            "raw_quotes_included": False,
        }
        report["source_quote_failure_diagnostics"] = {
            "cases": stages["location_failures"],
            "candidate_gate_failures": stages["candidate_gate_failures"],
            "interpretation": "定位失败不修补；每个错误引用都使完整候选失败。",
        }
        report["evaluation_stages"]["quote_selection_reply_valid"] = report[
            "evaluation_stages"
        ].pop("strict_grade_model_reply_valid")
        report["evaluation_stages"]["quote_selection_reply_valid"]["definition"] = (
            "独立GradeQuoteSelectionReply协议有效，尚不代表定位、分值或语义有效。"
        )
        report["evaluation_stages"]["rubric_score_constraints_valid"]["numerator"] = stages[
            "rubric_valid_cases"
        ]
        report["evaluation_stages"]["rubric_score_constraints_valid"]["denominator"] = stages[
            "candidate_outcome_cases"
        ]
        report["evaluation_stages"]["source_quote_validation"] = {
            "valid_candidate_outcome_cases": stages["quote_valid_cases"],
            "candidate_outcome_cases": stages["candidate_outcome_cases"],
            "valid_quote_references": stages["unique_exact_located_references"],
            "quote_references_checked": stages["selection_references"],
            "definition": "指定segment唯一精确定位后再次校验；正分仍须有效quote。",
        }
    zero_positive_cases = [
        item
        for item in report["semantic_score_risk_review"]["cases"]
        if Decimal(item["reference_total_score"]) == 0
        and item["unvalidated_response_total_score"] is not None
        and Decimal(item["unvalidated_response_total_score"]) > 0
    ]
    report["zero_reference_positive_score_risks"] = {
        "raw_intent_case_count": len(zero_positive_cases),
        "usable_candidate_case_count": sum(
            item["included_in_candidate_score_metrics"] for item in zero_positive_cases
        ),
        "cases": zero_positive_cases,
        "definition": "合成参考总分为0却有正分数值意图；协议失败也保留风险。",
    }
    return report


def compare_experiments(dataset, digest: str, strict, selection) -> dict:
    if (strict.mode, selection.mode) != ("strict", "quote_selection"):
        raise ValueError("comparison_requires_strict_then_quote_selection")
    for field in (
        "dataset_version",
        "dataset_sha256",
        "provider",
        "model_id",
        "generation_config_digest",
        "generation_options",
        "timeout_seconds",
        "invalid_response_retries",
    ):
        if getattr(strict, field) != getattr(selection, field):
            raise ValueError(f"paired_experiment_{field}_mismatch")
    reports = [
        evaluate_experiment(dataset, digest, predictions) for predictions in (strict, selection)
    ]
    by_id = [{item["case_id"]: item for item in report["per_case"]} for report in reports]
    requested_ids = [
        {
            item.case_id
            for item in predictions.cases
            if item.status not in {"not_requested", "excluded_uncertain"}
        }
        for predictions in (strict, selection)
    ]
    if requested_ids[0] != requested_ids[1]:
        raise ValueError("paired_experiment_requested_cases_mismatch")
    buckets = {
        "both_candidate": [],
        "strict_only": [],
        "selection_only": [],
        "neither_candidate": [],
    }
    paired_cases = []
    common_errors = [Decimal(0), Decimal(0)]
    for case_id in sorted(requested_ids[0]):
        a, b = [items[case_id] for items in by_id]
        successes = (a["status"] == "candidate", b["status"] == "candidate")
        bucket = {
            (True, True): "both_candidate",
            (True, False): "strict_only",
            (False, True): "selection_only",
            (False, False): "neither_candidate",
        }[successes]
        buckets[bucket].append(case_id)
        paired_cases.append(
            {
                "case_id": case_id,
                "strict_status": a["status"],
                "selection_status": b["status"],
                "strict_total": a.get("predicted_total_score"),
                "selection_total": b.get("predicted_total_score"),
                "reference_total": a.get("reference_total_score", b.get("reference_total_score")),
            }
        )
        if all(successes):
            for index, item in enumerate((a, b)):
                common_errors[index] += Decimal(item["total_absolute_error"])
    common_count = len(buckets["both_candidate"])
    return {
        "dataset_sha256": digest,
        "paired_requested_case_count": len(requested_ids[0]),
        "paired_outcomes": {
            key: {"count": len(ids), "case_ids": ids} for key, ids in buckets.items()
        },
        "common_candidate_total_mae": {
            "denominator": common_count,
            "strict": str(common_errors[0] / common_count) if common_count else None,
            "quote_selection": str(common_errors[1] / common_count) if common_count else None,
        },
        "paired_cases": paired_cases,
        "semantic_accuracy_claim": False,
        "interpretation": "单轮配对观测，不证明确定性提升；条件MAE的支持集可能不同。",
    }
