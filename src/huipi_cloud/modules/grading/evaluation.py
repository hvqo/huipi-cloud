"""Offline evaluation for synthetic rubric-grading references and predictions.

Reference labels live in a separate dataset document. Only the input fields are
copied into the in-memory GradeInputDocument sent to the existing P3-B prompt.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import (
    GradeProviderEndedResponseError,
    TextGradingProvider,
    TextGradingResponse,
)
from huipi_cloud.infrastructure.llm.deepseek import finish_reason_error, is_deepseek_endpoint
from huipi_cloud.modules.grading.prompts import PROMPT_VERSION
from huipi_cloud.modules.grading.proposal_errors import (
    GradeProposalError,
    GradeProposalInvalidEvidenceError,
    GradeProposalInvalidModelResponseError,
    GradeProposalInvalidRubricScoresError,
)
from huipi_cloud.modules.grading.proposal_protocol import (
    GradeModelReply,
    QuoteEvidenceReference,
)
from huipi_cloud.modules.grading.proposal_service import (
    _call_and_validate,
    _classify_reply_validation_error,
    _validate_candidate_scores,
    _validate_quote_reference,
)
from huipi_cloud.modules.grading.protocol import (
    AnswerKeySnapshot,
    GradeInputCore,
    GradeInputDocument,
    GradeInputEvidenceItem,
    GradeInputQuestionSnapshot,
    RubricCriterionSnapshot,
    SelectedAnswerSegment,
    json_sha256,
    rubric_sha256,
)

_UUID_NAMESPACE = UUID("0aa6db98-a0f0-4e0b-880d-b60b785f2efc")
_EVALUATION_SCHEMA = "huipi.grading.evaluation.predictions"
_SCORE_TOLERANCE = Decimal("0.50")
_PREDICTION_STATUSES = Literal[
    "candidate",
    "abstain",
    "protocol_error",
    "provider_timeout",
    "provider_error",
    "invalid_rubric_scores",
    "invalid_evidence",
    "input_too_large",
    "not_requested",
    "excluded_uncertain",
]
_PROTOCOL_DIAGNOSTIC_CATEGORIES = (
    "invalid_json",
    "missing_required_field",
    "unexpected_field",
    "invalid_outcome",
    "invalid_quote_length",
    "invalid_quote_offsets",
    "invalid_quote_text",
    "empty_quote",
    "invalid_no_evidence_shape",
    "invalid_score_value",
    "invalid_criterion_structure",
    "other_protocol_error",
)


class EvaluationRubricCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=2000)
    max_points: Decimal = Field(gt=0, max_digits=8, decimal_places=2, allow_inf_nan=False)


class EvaluationStudentSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["text", "formula_text"] = "text"
    page_index: int = Field(default=0, ge=0)
    text: str = Field(min_length=1, max_length=12000)


class ReferenceEvidenceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_index: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=2000)
    text_start: int | None = Field(default=None, ge=0)


class ReferenceCriterionScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_key: str = Field(min_length=1, max_length=64)
    awarded_points: Decimal = Field(ge=0, max_digits=8, decimal_places=2, allow_inf_nan=False)
    rationale: str = Field(min_length=1, max_length=2000)
    evidence: list[ReferenceEvidenceQuote] = Field(default_factory=list, max_length=30)


class RubricEvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    case_kind: str = Field(min_length=1, max_length=80)
    question_type: str = Field(min_length=1, max_length=30)
    stem: str = Field(min_length=1, max_length=4000)
    max_score: Decimal = Field(gt=0, max_digits=8, decimal_places=2, allow_inf_nan=False)
    answer_key: str = Field(min_length=1, max_length=4000)
    rubric: list[EvaluationRubricCriterion] = Field(min_length=1, max_length=30)
    student_segments: list[EvaluationStudentSegment] = Field(min_length=1, max_length=20)
    reference_status: Literal["included", "excluded_uncertain"]
    reference_total_score: Decimal | None = Field(
        default=None, ge=0, max_digits=8, decimal_places=2, allow_inf_nan=False
    )
    reference_criterion_scores: list[ReferenceCriterionScore] = Field(default_factory=list)
    reference_rationale: str | None = Field(default=None, max_length=2000)
    reference_exclusion_reason: str | None = Field(default=None, max_length=1000)
    label_provenance: Literal["synthetic_curated_reference"]
    difficulty: Literal["easy", "medium", "hard"]

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        rubric_by_key = {item.criterion_key: item for item in self.rubric}
        if len(rubric_by_key) != len(self.rubric):
            raise ValueError("rubric criterion keys must be unique")
        if sum((item.max_points for item in self.rubric), Decimal("0.00")) != self.max_score:
            raise ValueError("rubric maximums must sum to max_score")
        if self.reference_status == "excluded_uncertain":
            if (
                self.reference_total_score is not None
                or self.reference_criterion_scores
                or not self.reference_exclusion_reason
            ):
                raise ValueError("uncertain reference cases must be excluded without score labels")
            return self
        if (
            self.reference_total_score is None
            or self.reference_exclusion_reason is not None
            or not self.reference_rationale
        ):
            raise ValueError("included cases require complete reference labels and rationale")
        reference_by_key = {item.criterion_key: item for item in self.reference_criterion_scores}
        if len(reference_by_key) != len(self.reference_criterion_scores):
            raise ValueError("reference criterion keys must be unique")
        if set(reference_by_key) != set(rubric_by_key):
            raise ValueError("reference scores must cover each Rubric criterion exactly once")
        total = Decimal("0.00")
        for key, reference in reference_by_key.items():
            if reference.awarded_points > rubric_by_key[key].max_points:
                raise ValueError("reference criterion score exceeds its Rubric maximum")
            if reference.awarded_points > 0 and not reference.evidence:
                raise ValueError("positive reference scores require curated evidence quotes")
            for evidence in reference.evidence:
                if evidence.segment_index >= len(self.student_segments):
                    raise ValueError("reference evidence segment does not exist")
                source_text = self.student_segments[evidence.segment_index].text
                if evidence.text_start is None:
                    if source_text.count(evidence.quote) != 1:
                        raise ValueError(
                            "reference quote must be unique or provide text_start"
                        )
                elif source_text[
                    evidence.text_start : evidence.text_start + len(evidence.quote)
                ] != evidence.quote:
                    raise ValueError("reference quote offset does not match its source segment")
            total += reference.awarded_points
        if total != self.reference_total_score or total > self.max_score:
            raise ValueError("reference total must equal criterion scores and fit max_score")
        return self


class RubricEvaluationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_name: Literal["huipi-rubric-candidate-grading"]
    dataset_version: str = Field(min_length=1, max_length=32)
    source: Literal["synthetic_curated_reference"]
    description: str = Field(min_length=1, max_length=1000)
    cases: list[RubricEvaluationCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_cases(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("dataset case IDs must be unique")
        return self


class EvaluationPredictionCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=100)
    status: _PREDICTION_STATUSES
    response_content: str | None = Field(default=None, max_length=128 * 1024)
    error_code: str | None = Field(default=None, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    elapsed_ms: int | None = Field(default=None, ge=0)
    response_model_id: str | None = Field(default=None, min_length=1, max_length=200)
    finish_reason: str | None = Field(default=None, max_length=64)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.status in {"candidate", "abstain", "protocol_error"} and (
            self.response_content is None
        ):
            raise ValueError("model replies require the raw response content")
        if self.status in {
            "candidate",
            "abstain",
            "not_requested",
            "excluded_uncertain",
        }:
            if self.error_code is not None:
                raise ValueError("successful or excluded predictions cannot carry an error code")
        elif not self.error_code:
            raise ValueError("failed predictions require a stable error code")
        if (
            self.status in {"not_requested", "excluded_uncertain"}
            and self.response_content is not None
        ):
            raise ValueError("non-requested predictions cannot contain a model response")
        return self


class RubricEvaluationPredictions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["huipi.grading.evaluation.predictions"] = _EVALUATION_SCHEMA
    schema_version: Literal["1.0"] = "1.0"
    dataset_version: str = Field(min_length=1, max_length=32)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: Literal["ollama_native", "openai_compatible"]
    model_id: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=64)
    generation_config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    thinking_mode: Literal["disabled", "enabled"] | None = None
    reasoning_effort: Literal["low", "high", "max"] | None = None
    configured_reasoning_effort: Literal["low", "high", "max"] | None = None
    max_output_tokens: int | None = Field(default=None, ge=64, le=16384)
    invalid_response_retries: int = Field(ge=0, le=1)
    timeout_seconds: float = Field(gt=0, le=180)
    created_at: datetime
    cases: list[EvaluationPredictionCase] = Field(min_length=1, max_length=100)


def load_dataset(path: Path) -> tuple[RubricEvaluationDataset, str]:
    """Load and strictly validate a dataset; return its SHA-256 over exact file bytes."""

    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    digest_path = path.with_suffix(path.suffix + ".sha256")
    if digest_path.exists():
        tokens = digest_path.read_text(encoding="ascii").strip().split()
        if not tokens or len(tokens[0]) != 64 or any(
            character not in "0123456789abcdef" for character in tokens[0]
        ):
            raise ValueError("dataset_sha256_manifest_invalid")
        expected = tokens[0]
        if expected != digest:
            raise ValueError("dataset_sha256_does_not_match_manifest")
    dataset = RubricEvaluationDataset.model_validate_json(payload)
    return dataset, digest


def build_grade_input(case: RubricEvaluationCase) -> GradeInputDocument:
    """Build a transient GradeInput fixture without copying any reference labels."""

    def stable_id(name: str) -> UUID:
        return uuid5(_UUID_NAMESPACE, f"{case.case_id}:{name}")

    question_id = stable_id("question")
    segments: list[SelectedAnswerSegment] = []
    evidence_manifest: list[GradeInputEvidenceItem] = []
    for index, source in enumerate(case.student_segments):
        block_id = stable_id(f"block:{index}")
        page_index = source.page_index
        pointer = f"/pages/{page_index}/blocks/{index}/content"
        digest = hashlib.sha256(source.text.encode("utf-8")).hexdigest()
        segments.append(
            SelectedAnswerSegment(
                segment_index=index,
                kind=source.kind,
                content_pointer=pointer,
                page_index=page_index,
                page_number=page_index + 1,
                source_block_id=block_id,
                reading_order=index,
                text_start=0,
                text_end=len(source.text),
                text=source.text,
                text_sha256=digest,
            )
        )
        evidence_manifest.append(
            GradeInputEvidenceItem(
                content_pointer=pointer,
                page_index=page_index,
                page_number=page_index + 1,
                source_block_id=block_id,
                reading_order=index,
                normalized_type=source.kind,
                text_start=0,
                text_end=len(source.text),
                representation=source.kind,
            )
        )
    answer_key = AnswerKeySnapshot(
        answer_key_id=stable_id("answer-key"),
        question_id=question_id,
        source="teacher",
        answer_content=case.answer_key,
        sha256=hashlib.sha256(case.answer_key.encode("utf-8")).hexdigest(),
    )
    rubric = [
        RubricCriterionSnapshot(
            criterion_id=stable_id(f"criterion:{item.criterion_key}"),
            question_id=question_id,
            description=item.description,
            points=item.max_points,
            sort_order=index + 1,
        )
        for index, item in enumerate(case.rubric)
    ]
    core = GradeInputCore(
        request_id=stable_id("request"),
        submission_id=stable_id("submission"),
        assignment_id=stable_id("assignment"),
        question=GradeInputQuestionSnapshot(
            question_id=question_id,
            question_number=1,
            question_type=case.question_type,
            stem=case.stem,
            max_score=case.max_score,
        ),
        canonical_artifact_id=stable_id("canonical"),
        canonical_sha256=hashlib.sha256(f"canonical:{case.case_id}".encode()).hexdigest(),
        canonical_schema_version="1.0",
        canonical_source_sha256=hashlib.sha256(f"source:{case.case_id}".encode()).hexdigest(),
        alignment_artifact_id=stable_id("alignment"),
        alignment_sha256=hashlib.sha256(f"alignment:{case.case_id}".encode()).hexdigest(),
        aligner_version="evaluation-fixture",
        assignment_questions_digest=hashlib.sha256(b"synthetic-question-set").hexdigest(),
        review_decision_id=stable_id("review-decision"),
        review_revision=1,
        review_request_sha256=hashlib.sha256(f"review:{case.case_id}".encode()).hexdigest(),
        review_schema_version="1.0",
        review_evidence_sha256=hashlib.sha256(f"evidence:{case.case_id}".encode()).hexdigest(),
        excluded_prompt_regions_sha256=hashlib.sha256(b"no-excluded-regions").hexdigest(),
        source_file_sha256=hashlib.sha256(f"synthetic-source:{case.case_id}".encode()).hexdigest(),
        answer_key=answer_key,
        rubric_snapshot=rubric,
        rubric_sha256=rubric_sha256(rubric),
        selected_answer_segments=segments,
        evidence_manifest=evidence_manifest,
    )
    return GradeInputDocument(
        **core.model_dump(),
        artifact_id=stable_id("grade-input-artifact"),
        input_sha256=json_sha256(core),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def evaluate_predictions(
    dataset: RubricEvaluationDataset,
    dataset_sha256: str,
    predictions: RubricEvaluationPredictions,
    *,
    config: Settings | None = None,
    reply_evaluator: Callable[
        [RubricEvaluationCase, EvaluationPredictionCase, Settings],
        tuple[str, GradeModelReply | None, str | None],
    ] | None = None,
) -> dict[str, object]:
    """Revalidate saved provider outputs and calculate reproducible metrics."""

    if predictions.dataset_version != dataset.dataset_version:
        raise ValueError("prediction_dataset_version_mismatch")
    if predictions.dataset_sha256 != dataset_sha256:
        raise ValueError("prediction_dataset_sha256_mismatch")
    by_case_id: dict[str, EvaluationPredictionCase] = {}
    for prediction in predictions.cases:
        if prediction.case_id in by_case_id:
            raise ValueError("duplicate_prediction_case_id")
        by_case_id[prediction.case_id] = prediction
    expected_ids = {case.case_id for case in dataset.cases}
    if set(by_case_id) != expected_ids:
        raise ValueError("prediction_case_set_mismatch")

    selected_config = config or Settings()
    active_cases = [case for case in dataset.cases if case.reference_status == "included"]
    excluded_cases = [case for case in dataset.cases if case.reference_status != "included"]
    requested_cases = [
        case for case in active_cases if by_case_id[case.case_id].status != "not_requested"
    ]
    derived: dict[str, tuple[str, GradeModelReply | None, str | None]] = {}
    for case in dataset.cases:
        prediction = by_case_id[case.case_id]
        if case.reference_status == "excluded_uncertain":
            if prediction.status != "excluded_uncertain":
                raise ValueError("excluded_reference_must_not_have_prediction")
            derived[case.case_id] = ("excluded_uncertain", None, case.reference_exclusion_reason)
            continue
        if prediction.status == "excluded_uncertain":
            raise ValueError("included_reference_cannot_be_marked_excluded")
        derived[case.case_id] = (reply_evaluator or _derive_prediction)(
            case, prediction, selected_config
        )

    valid_candidate_cases: list[tuple[RubricEvaluationCase, GradeModelReply]] = []
    abstention_count = 0
    status_counts: Counter[str] = Counter()
    quote_counts = Counter()
    per_case: list[dict[str, object]] = []
    exact_total_score_count = 0
    within_tolerance_count = 0
    total_absolute_error = Decimal("0.00")
    total_over_count = 0
    total_under_count = 0
    criterion_absolute_error = Decimal("0.00")
    criterion_exact_count = 0
    criterion_over_count = 0
    criterion_under_count = 0
    criterion_count = 0
    criterion_metrics: dict[str, dict[str, int | Decimal]] = {}
    offsetting_case_ids: list[str] = []
    failure_cases: list[dict[str, object]] = []
    overlap_gold_chars = overlap_model_chars = overlap_intersection_chars = 0
    prompt_token_total = completion_token_total = 0
    prompt_token_count = completion_token_count = 0
    elapsed_total_ms = elapsed_count = 0

    for case in dataset.cases:
        prediction = by_case_id[case.case_id]
        status, reply, error_code = derived[case.case_id]
        if case.reference_status != "included":
            per_case.append(
                {
                    "case_id": case.case_id,
                    "case_kind": case.case_kind,
                    "status": status,
                    "included_in_metrics": False,
                    "reason": error_code,
                }
            )
            continue
        if status not in {"not_requested"}:
            status_counts[status] += 1
            if prediction.prompt_tokens is not None:
                prompt_token_total += prediction.prompt_tokens
                prompt_token_count += 1
            if prediction.completion_tokens is not None:
                completion_token_total += prediction.completion_tokens
                completion_token_count += 1
            if prediction.elapsed_ms is not None:
                elapsed_total_ms += prediction.elapsed_ms
                elapsed_count += 1
        if reply is not None and reply.outcome == "candidate":
            _count_quote_evidence(reply, case, selected_config, quote_counts)
        if status == "candidate" and reply is not None:
            valid_candidate_cases.append((case, reply))
            predicted_total = _reply_total(reply)
            expected_total = case.reference_total_score
            assert expected_total is not None
            delta = predicted_total - expected_total
            total_absolute_error += abs(delta)
            exact = delta == 0
            within = abs(delta) <= _SCORE_TOLERANCE
            exact_total_score_count += int(exact)
            within_tolerance_count += int(within)
            total_over_count += int(delta > 0)
            total_under_count += int(delta < 0)
            reference = {item.criterion_key: item for item in case.reference_criterion_scores}
            scores_by_key = {
                criterion.criterion_key: next(
                    score
                    for score in reply.criterion_scores
                    if score.criterion_id == _criterion_uuid(case, criterion.criterion_key)
                )
                for criterion in case.rubric
            }
            criterion_deltas: list[dict[str, str]] = []
            case_has_criterion_error = False
            for criterion in case.rubric:
                expected = reference[criterion.criterion_key].awarded_points
                actual = scores_by_key[criterion.criterion_key].awarded_points
                criterion_delta = actual - expected
                criterion_count += 1
                criterion_absolute_error += abs(criterion_delta)
                criterion_exact_count += int(criterion_delta == 0)
                criterion_over_count += int(criterion_delta > 0)
                criterion_under_count += int(criterion_delta < 0)
                item_metrics = criterion_metrics.setdefault(
                    f"{case.case_id}/{criterion.criterion_key}",
                    {
                        "count": 0,
                        "absolute_error_sum": Decimal("0.00"),
                        "exact_count": 0,
                        "over_count": 0,
                        "under_count": 0,
                    },
                )
                item_metrics["count"] = int(item_metrics["count"]) + 1
                item_metrics["absolute_error_sum"] = (
                    item_metrics["absolute_error_sum"] + abs(criterion_delta)
                )
                item_metrics["exact_count"] = int(item_metrics["exact_count"]) + int(
                    criterion_delta == 0
                )
                item_metrics["over_count"] = int(item_metrics["over_count"]) + int(
                    criterion_delta > 0
                )
                item_metrics["under_count"] = int(item_metrics["under_count"]) + int(
                    criterion_delta < 0
                )
                if criterion_delta != 0:
                    case_has_criterion_error = True
                    criterion_deltas.append(
                        {
                            "criterion_key": criterion.criterion_key,
                            "reference": _decimal_text(expected),
                            "predicted": _decimal_text(actual),
                            "delta": _decimal_text(criterion_delta),
                        }
                    )
            if exact and case_has_criterion_error:
                offsetting_case_ids.append(case.case_id)
            overlap = _evidence_overlap(case, reply)
            overlap_gold_chars += overlap[0]
            overlap_model_chars += overlap[1]
            overlap_intersection_chars += overlap[2]
            case_entry: dict[str, object] = {
                "case_id": case.case_id,
                "case_kind": case.case_kind,
                "difficulty": case.difficulty,
                "status": status,
                "included_in_metrics": True,
                "reference_total_score": _decimal_text(expected_total),
                "predicted_total_score": _decimal_text(predicted_total),
                "total_absolute_error": _decimal_text(abs(delta)),
                "within_tolerance": within,
                "criterion_errors": criterion_deltas,
                "failure_categories": _score_failure_categories(
                    exact=exact,
                    within=within,
                    criterion_error=case_has_criterion_error,
                    over=delta > 0,
                    under=delta < 0,
                ),
                **_runtime_detail(prediction),
            }
            if not exact or case_has_criterion_error:
                failure_cases.append(case_entry)
            per_case.append(case_entry)
        elif status == "abstain":
            abstention_count += 1
            entry = {
                "case_id": case.case_id,
                "case_kind": case.case_kind,
                "difficulty": case.difficulty,
                "status": status,
                "included_in_metrics": True,
                "failure_categories": ["model_abstained_on_scored_reference"],
                "reason": "model_abstention_reason_redacted",
                **_runtime_detail(prediction),
            }
            failure_cases.append(entry)
            per_case.append(entry)
        elif status == "not_requested":
            per_case.append(
                {
                    "case_id": case.case_id,
                    "case_kind": case.case_kind,
                    "status": status,
                    "included_in_metrics": False,
                    "reason": "outside_requested_subset",
                }
            )
        else:
            entry = {
                "case_id": case.case_id,
                "case_kind": case.case_kind,
                "difficulty": case.difficulty,
                "status": status,
                "included_in_metrics": True,
                "reason": error_code,
                "failure_categories": [_failure_category(status)],
                **_runtime_detail(prediction),
            }
            failure_cases.append(entry)
            per_case.append(entry)

    score_denominator = len(valid_candidate_cases)
    criterion_mae = _mean_text(criterion_absolute_error, criterion_count)
    total_mae = _mean_text(total_absolute_error, score_denominator)
    requested_count = len(requested_cases)
    candidate_count = len(valid_candidate_cases)
    protocol_valid_count = sum(
        status_counts[status]
        for status in {"candidate", "abstain", "invalid_evidence", "invalid_rubric_scores"}
    )
    criterion_by_key = {
        key: {
            "count": int(values["count"]),
            "absolute_error_sum": _decimal_text(values["absolute_error_sum"]),
            "mae": _mean_text(
                values["absolute_error_sum"],
                int(values["count"]),
            ),
            "exact": {"numerator": int(values["exact_count"]), "denominator": int(values["count"])},
            "over_count": int(values["over_count"]),
            "under_count": int(values["under_count"]),
        }
        for key, values in sorted(criterion_metrics.items())
    }
    requested_prediction_cases = [
        by_case_id[case.case_id]
        for case in active_cases
        if by_case_id[case.case_id].status != "not_requested"
    ]
    response_predictions = [
        prediction
        for prediction in requested_prediction_cases
        if prediction.response_content is not None
    ]
    json_parseable_count = sum(
        _is_json_parseable(prediction.response_content)
        for prediction in response_predictions
    )
    candidate_outcome_pairs = [
        (case, reply)
        for case in active_cases
        for status, reply, _ in [derived[case.case_id]]
        if reply is not None and reply.outcome == "candidate"
    ]
    rubric_valid_pairs = [
        (case, reply)
        for case, reply in candidate_outcome_pairs
        if _rubric_score_constraints_valid(case, reply)
    ]
    quote_valid_case_count = 0
    stage_quote_refs_checked = 0
    stage_quote_refs_valid = 0
    for case, reply in candidate_outcome_pairs:
        valid, checked, valid_count = _candidate_quote_sources_valid(case, reply, selected_config)
        quote_valid_case_count += int(valid)
        stage_quote_refs_checked += checked
        stage_quote_refs_valid += valid_count

    protocol_failure_diagnostics = (
        _protocol_failure_diagnostics(dataset, by_case_id)
        if reply_evaluator is None
        else {"interpretation": "实验协议的诊断由对应实验评测器提供。"}
    )
    source_quote_failure_diagnostics = _source_quote_failure_diagnostics(
        dataset, derived, selected_config
    )
    semantic_score_risks = _semantic_score_risk_cases(dataset, by_case_id)
    report = {
        "dataset_version": dataset.dataset_version,
        "dataset_sha256": dataset_sha256,
        "prediction_model": {
            "provider": predictions.provider,
            "model_id": predictions.model_id,
            "prompt_version": predictions.prompt_version,
            "generation_config_digest": predictions.generation_config_digest,
            "timeout_seconds": predictions.timeout_seconds,
            "invalid_response_retries": predictions.invalid_response_retries,
        },
        "reference_source": "synthetic_curated_reference",
        "score_tolerance": _decimal_text(_SCORE_TOLERANCE),
        "counts": {
            "dataset_cases": len(dataset.cases),
            "included_reference_cases": len(active_cases),
            "excluded_uncertain_reference_cases": len(excluded_cases),
            "requested_cases": requested_count,
            "not_requested_cases": len(active_cases) - requested_count,
            "valid_candidate_cases": candidate_count,
            "abstention_cases": abstention_count,
            "protocol_valid_replies": protocol_valid_count,
            "prediction_status_counts": dict(sorted(status_counts.items())),
        },
        "evaluation_stages": {
            "provider_response_received": {
                "numerator": len(response_predictions),
                "denominator": requested_count,
            },
            "json_parseable": {
                "numerator": json_parseable_count,
                "denominator": len(response_predictions),
            },
            "strict_grade_model_reply_valid": {
                "numerator": protocol_valid_count,
                "denominator": len(response_predictions),
                "definition": "GradeModelReply结构协议通过，尚不代表Rubric、quote或语义检查通过。",
            },
            "rubric_score_constraints_valid": {
                "numerator": len(rubric_valid_pairs),
                "denominator": len(candidate_outcome_pairs),
                "definition": "只诊断Criterion集合、分值范围和总分算术；不替代P3-B接受校验。",
            },
            "source_quote_validation": {
                "valid_candidate_outcome_cases": quote_valid_case_count,
                "candidate_outcome_cases": len(candidate_outcome_pairs),
                "valid_quote_references": stage_quote_refs_valid,
                "quote_references_checked": stage_quote_refs_checked,
                "definition": (
                    "按P3-B原文和Unicode码点半开区间校验；"
                    "正分还必须至少有一条有效quote。"
                ),
            },
            "final_usable_candidate": {
                "numerator": candidate_count,
                "denominator": requested_count,
            },
            "candidate_total_score_comparison": {
                "exact_numerator": exact_total_score_count,
                "denominator": score_denominator,
                "mae": total_mae,
                "semantic_accuracy_claim": False,
            },
            "rationale_semantic_adequacy": {
                "automated_result": "not_evaluated",
                "human_review_required": True,
            },
        },
        "protocol_failure_diagnostics": protocol_failure_diagnostics,
        "source_quote_failure_diagnostics": source_quote_failure_diagnostics,
        "semantic_score_risk_review": semantic_score_risks,
        "runtime_usage": {
            "elapsed_ms_total": elapsed_total_ms,
            "elapsed_ms_count": elapsed_count,
            "elapsed_ms_mean": (
                round(elapsed_total_ms / elapsed_count, 2) if elapsed_count else None
            ),
            "prompt_tokens_total": prompt_token_total,
            "prompt_token_count": prompt_token_count,
            "completion_tokens_total": completion_token_total,
            "completion_token_count": completion_token_count,
        },
        "coverage": {
            "candidate_over_requested": _ratio_text(candidate_count, requested_count),
            "candidate_over_included_dataset": _ratio_text(candidate_count, len(active_cases)),
            "abstention_over_requested": _ratio_text(abstention_count, requested_count),
            "protocol_valid_over_requested": _ratio_text(protocol_valid_count, requested_count),
            "protocol_error_over_requested": _ratio_text(
                status_counts["protocol_error"], requested_count
            ),
            "invalid_evidence_over_requested": _ratio_text(
                status_counts["invalid_evidence"], requested_count
            ),
            "invalid_rubric_scores_over_requested": _ratio_text(
                status_counts["invalid_rubric_scores"], requested_count
            ),
            "provider_failure_over_requested": _ratio_text(
                status_counts["provider_error"]
                + status_counts["provider_timeout"]
                + status_counts["input_too_large"],
                requested_count,
            ),
        },
        "total_score": {
            "sample_count": score_denominator,
            "absolute_error_sum": _decimal_text(total_absolute_error),
            "mae": total_mae,
            "mae_denominator": score_denominator,
            "exact": {"numerator": exact_total_score_count, "denominator": score_denominator},
            "within_tolerance": {
                "numerator": within_tolerance_count,
                "denominator": score_denominator,
            },
            "over_count": total_over_count,
            "under_count": total_under_count,
        },
        "criterion_score": {
            "sample_count": criterion_count,
            "absolute_error_sum": _decimal_text(criterion_absolute_error),
            "mae": criterion_mae,
            "mae_denominator": criterion_count,
            "exact": {"numerator": criterion_exact_count, "denominator": criterion_count},
            "over_count": criterion_over_count,
            "under_count": criterion_under_count,
            "by_case_criterion": criterion_by_key,
            "offsetting_error_cases": len(offsetting_case_ids),
            "offsetting_error_case_ids": offsetting_case_ids,
        },
        "evidence": {
            "quote_references_checked": quote_counts["checked"],
            "valid_quote_references": quote_counts["valid"],
            "invalid_quote_references": quote_counts["invalid"],
            "positive_scores_without_quote": quote_counts["positive_without_quote"],
            "positive_criterion_scores": quote_counts["positive"],
            "positive_scores_with_valid_quote": quote_counts["positive_with_quote"],
            "reference_quote_overlap_characters": {
                "gold": overlap_gold_chars,
                "predicted": overlap_model_chars,
                "intersection": overlap_intersection_chars,
                "precision": _ratio_text(overlap_intersection_chars, overlap_model_chars),
                "recall": _ratio_text(overlap_intersection_chars, overlap_gold_chars),
                "iou": _ratio_text(
                    overlap_intersection_chars,
                    overlap_gold_chars + overlap_model_chars - overlap_intersection_chars,
                ),
            },
            "semantic_support": "requires_human_review",
        },
        "failure_cases": failure_cases,
        "per_case": per_case,
    }
    if predictions.max_output_tokens is not None:
        report["prediction_model"].update({
            "max_output_tokens": predictions.max_output_tokens,
            "thinking_mode": predictions.thinking_mode,
            "reasoning_effort": predictions.reasoning_effort,
            "configured_reasoning_effort": predictions.configured_reasoning_effort,
        })
    if any(item.response_model_id is not None for item in predictions.cases):
        report["provider_call_metadata"] = [
            {
                "case_id": item.case_id,
                "actual_model_id": item.response_model_id,
                "finish_reason": item.finish_reason,
                "reasoning_tokens": item.reasoning_tokens,
                "cache_hit_tokens": item.cache_hit_tokens,
                "cache_miss_tokens": item.cache_miss_tokens,
            }
            for item in predictions.cases
            if item.response_model_id is not None
        ]
    return report


def _is_json_parseable(content: str | None) -> bool:
    if content is None:
        return False
    try:
        json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return False
    return True


def _rubric_score_constraints_valid(
    case: RubricEvaluationCase,
    reply: GradeModelReply,
) -> bool:
    if reply.outcome != "candidate" or reply.total_score is None:
        return False
    expected = {_criterion_uuid(case, item.criterion_key): item for item in case.rubric}
    actual = {item.criterion_id: item for item in reply.criterion_scores}
    if len(actual) != len(reply.criterion_scores) or set(actual) != set(expected):
        return False
    total = Decimal("0.00")
    for criterion_id, score in actual.items():
        points = expected[criterion_id].max_points
        if score.awarded_points < 0 or score.awarded_points > points:
            return False
        total += score.awarded_points
    return total == reply.total_score and total <= case.max_score


def _candidate_quote_sources_valid(
    case: RubricEvaluationCase,
    reply: GradeModelReply,
    config: Settings,
) -> tuple[bool, int, int]:
    if reply.outcome != "candidate":
        return False, 0, 0
    document = build_grade_input(case)
    all_valid = True
    checked = valid_count = 0
    for score in reply.criterion_scores:
        has_valid_quote = False
        for evidence in score.evidence_references:
            if not isinstance(evidence, QuoteEvidenceReference):
                continue
            checked += 1
            try:
                _validate_quote_reference(evidence, document, config)
            except GradeProposalInvalidEvidenceError:
                all_valid = False
            else:
                valid_count += 1
                has_valid_quote = True
        if score.awarded_points > 0 and not has_valid_quote:
            all_valid = False
    return all_valid, checked, valid_count


def _protocol_failure_diagnostics(
    dataset: RubricEvaluationDataset,
    predictions_by_id: dict[str, EvaluationPredictionCase],
) -> dict[str, object]:
    category_case_ids: dict[str, set[str]] = {
        category: set() for category in _PROTOCOL_DIAGNOSTIC_CATEGORIES
    }
    category_occurrences: Counter[str] = Counter()
    case_diagnostics: list[dict[str, object]] = []
    for case in dataset.cases:
        prediction = predictions_by_id[case.case_id]
        if case.reference_status != "included" or prediction.status != "protocol_error":
            continue
        diagnosed = _diagnose_protocol_reply(case, prediction.response_content)
        for category, occurrences in diagnosed["category_occurrences"].items():
            if occurrences > 0:
                category_case_ids[category].add(case.case_id)
                category_occurrences[category] += occurrences
        case_diagnostics.append(
            {
                "case_id": case.case_id,
                "categories": sorted(diagnosed["category_occurrences"]),
                "occurrences_by_category": dict(sorted(diagnosed["category_occurrences"].items())),
                "quote_checks": diagnosed["quote_checks"],
            }
        )
    return {
        "case_count": len(case_diagnostics),
        "occurrence_count": sum(category_occurrences.values()),
        "count_definition": {
            "case_count": "一个案例在某类别中最多计一次。",
            "occurrence_count": "该类别中独立字段或引用位置的次数；类别之间可重叠。",
        },
        "diagnostic_categories": {
            category: {
                "case_count": len(category_case_ids[category]),
                "occurrence_count": category_occurrences[category],
            }
            for category in _PROTOCOL_DIAGNOSTIC_CATEGORIES
        },
        "quote_source_match_summary": _quote_match_summary(case_diagnostics),
        "cases": case_diagnostics,
        "raw_quote_text_included": False,
    }


def _source_quote_failure_diagnostics(
    dataset: RubricEvaluationDataset,
    derived: dict[str, tuple[str, GradeModelReply | None, str | None]],
    config: Settings,
) -> dict[str, object]:
    categories = (
        "invalid_quote_offsets",
        "invalid_quote_text",
        "positive_score_without_quote",
    )
    case_ids: dict[str, set[str]] = {category: set() for category in categories}
    occurrences: Counter[str] = Counter()
    cases: list[dict[str, object]] = []
    for case in dataset.cases:
        if case.reference_status != "included":
            continue
        status, reply, _ = derived[case.case_id]
        if reply is None or reply.outcome != "candidate":
            continue
        local_counts: Counter[str] = Counter()
        quote_checks: list[dict[str, object]] = []
        document = build_grade_input(case)
        for criterion_index, score in enumerate(reply.criterion_scores):
            has_valid_quote = False
            for evidence_index, evidence in enumerate(score.evidence_references):
                if not isinstance(evidence, QuoteEvidenceReference):
                    continue
                try:
                    _validate_quote_reference(evidence, document, config)
                except GradeProposalInvalidEvidenceError as error:
                    if error.reason_code in {
                        "evidence_segment_not_found",
                        "evidence_range_out_of_bounds",
                    }:
                        category = "invalid_quote_offsets"
                        match_count = None
                    elif error.reason_code == "evidence_quote_mismatch":
                        segment = document.selected_answer_segments[evidence.segment_index]
                        match_count = _count_overlapping_occurrences(segment.text, evidence.quote)
                        category = "invalid_quote_offsets" if match_count else "invalid_quote_text"
                    else:
                        category = "invalid_quote_text"
                        match_count = None
                    local_counts[category] += 1
                    quote_checks.append(
                        {
                            "criterion_index": criterion_index,
                            "evidence_index": evidence_index,
                            "segment_index": evidence.segment_index,
                            "quote_codepoints": len(evidence.quote),
                            "claimed_span_codepoints": evidence.text_end - evidence.text_start,
                            "source_exact_match_count": match_count,
                            "matches_claimed_offsets": False,
                        }
                    )
                else:
                    has_valid_quote = True
                    quote_checks.append(
                        {
                            "criterion_index": criterion_index,
                            "evidence_index": evidence_index,
                            "segment_index": evidence.segment_index,
                            "quote_codepoints": len(evidence.quote),
                            "claimed_span_codepoints": evidence.text_end - evidence.text_start,
                            "source_exact_match_count": 1,
                            "matches_claimed_offsets": True,
                        }
                    )
            if score.awarded_points > 0 and not has_valid_quote:
                local_counts["positive_score_without_quote"] += 1
        for category, count in local_counts.items():
            if count:
                case_ids[category].add(case.case_id)
                occurrences[category] += count
        if local_counts:
            cases.append(
                {
                    "case_id": case.case_id,
                    "stored_status": status,
                    "categories": sorted(local_counts),
                    "occurrences_by_category": dict(sorted(local_counts.items())),
                    "quote_checks": quote_checks,
                }
            )
    return {
        "case_count": len(cases),
        "occurrence_count": sum(occurrences.values()),
        "count_definition": {
            "case_count": "具有至少一项来源证据问题的不同案例数。",
            "occurrence_count": "问题引用或正分 Criterion 的个数；类别之间可重叠。",
        },
        "diagnostic_categories": {
            category: {
                "case_count": len(case_ids[category]),
                "occurrence_count": occurrences[category],
            }
            for category in categories
        },
        "quote_source_match_summary": _quote_match_summary(cases),
        "cases": cases,
        "raw_quote_text_included": False,
    }


def _diagnose_protocol_reply(
    case: RubricEvaluationCase,
    response_content: str | None,
) -> dict[str, object]:
    counts: Counter[str] = Counter()
    seen: set[tuple[str, str]] = set()
    quote_checks: list[dict[str, object]] = []

    def add(category: str, occurrence_key: str) -> None:
        if category not in counts and category not in _PROTOCOL_DIAGNOSTIC_CATEGORIES:
            category = "other_protocol_error"
        identity = (category, occurrence_key)
        if identity not in seen:
            seen.add(identity)
            counts[category] += 1

    try:
        raw = json.loads(response_content) if response_content is not None else None
    except (json.JSONDecodeError, TypeError):
        add("invalid_json", "response")
        return {"category_occurrences": counts, "quote_checks": quote_checks}
    if not isinstance(raw, dict):
        add("other_protocol_error", "root_not_object")
        return {"category_occurrences": counts, "quote_checks": quote_checks}

    try:
        GradeModelReply.model_validate(raw)
    except ValidationError as error:
        for detail in error.errors(include_input=False):
            location = tuple(detail.get("loc", ()))
            error_type = str(detail.get("type", ""))
            if "evidence_references" in location:
                continue
            if error_type == "extra_forbidden":
                # Root and nested extras are collected from the parsed object
                # below so one extra key is one diagnostic occurrence.
                continue
            if error_type == "missing" and (
                "criterion_scores" in location
                or location in {("schema_version",), ("outcome",)}
            ):
                continue
            if error_type == "missing":
                category = "missing_required_field"
            elif error_type == "extra_forbidden":
                category = "unexpected_field"
            elif "outcome" in location:
                category = "invalid_outcome"
            elif not location and error_type == "value_error":
                # The root validator's exact missing/contradictory fields are
                # diagnosed from the parsed object below.
                continue
            elif "awarded_points" in location or "total_score" in location:
                category = "invalid_score_value"
            elif "criterion_scores" in location or "criterion_id" in location:
                category = "invalid_criterion_structure"
            else:
                category = "other_protocol_error"
            add(category, f"schema:{'.'.join(map(str, location))}:{error_type}")

    outcome = raw.get("outcome")
    if not isinstance(outcome, str) or outcome not in {"candidate", "abstain"}:
        add("invalid_outcome", "outcome")
    if "schema_version" not in raw:
        add("missing_required_field", "schema_version")
    if "outcome" not in raw:
        add("missing_required_field", "outcome")
    if outcome == "candidate":
        if "total_score" not in raw or raw.get("total_score") is None:
            add("missing_required_field", "total_score")
        if "criterion_scores" not in raw:
            add("missing_required_field", "criterion_scores")
        elif raw.get("criterion_scores") == []:
            add("invalid_criterion_structure", "criterion_scores:empty_candidate")
        if raw.get("abstention_reason") is not None:
            add("invalid_outcome", "candidate_with_abstention_reason")
    elif outcome == "abstain":
        reason = raw.get("abstention_reason")
        if not isinstance(reason, str) or not reason.strip():
            add("missing_required_field", "abstention_reason")
        if raw.get("total_score") is not None or raw.get("criterion_scores"):
            add("invalid_outcome", "abstain_with_scores")
    allowed_reply_fields = {
        "schema_version",
        "outcome",
        "total_score",
        "criterion_scores",
        "abstention_reason",
        "reason_codes",
    }
    for field_name in raw.keys() - allowed_reply_fields:
        add("unexpected_field", f"root:{field_name}")

    criterion_scores = raw.get("criterion_scores", [])
    if not isinstance(criterion_scores, list):
        add("invalid_criterion_structure", "criterion_scores:not_list")
        criterion_scores = []
    for criterion_index, criterion in enumerate(criterion_scores):
        criterion_path = f"criterion:{criterion_index}"
        if not isinstance(criterion, dict):
            add("invalid_criterion_structure", criterion_path)
            continue
        allowed_criterion_fields = {
            "criterion_id",
            "awarded_points",
            "rationale",
            "evidence_references",
        }
        for field_name in criterion.keys() - allowed_criterion_fields:
            add("unexpected_field", f"{criterion_path}:{field_name}")
        for required in ("criterion_id", "awarded_points", "rationale", "evidence_references"):
            if required not in criterion:
                add("missing_required_field", f"{criterion_path}:{required}")
        references = criterion.get("evidence_references", [])
        if not isinstance(references, list):
            add("invalid_criterion_structure", f"{criterion_path}:evidence_not_list")
            continue
        if not references or len(references) > 50:
            add("invalid_criterion_structure", f"{criterion_path}:evidence_count")
        for reference_index, reference in enumerate(references):
            reference_path = f"{criterion_path}:reference:{reference_index}"
            if not isinstance(reference, dict):
                add("invalid_criterion_structure", reference_path)
                continue
            kind = reference.get("kind")
            if kind == "no_evidence":
                allowed = {"kind", "reason"}
                reason = reference.get("reason")
                if (
                    set(reference) != allowed
                    or not isinstance(reason, str)
                    or not reason.strip()
                    or len(reason) > 1000
                ):
                    add("invalid_no_evidence_shape", reference_path)
                continue
            if kind != "quote" and not ({"quote", "text_start", "text_end"} & set(reference)):
                add("invalid_criterion_structure", reference_path)
                continue
            if kind != "quote":
                add("invalid_criterion_structure", f"{reference_path}:kind")
            allowed_quote_fields = {
                "kind",
                "segment_index",
                "text_start",
                "text_end",
                "quote",
            }
            for field_name in reference.keys() - allowed_quote_fields:
                add("unexpected_field", f"{reference_path}:{field_name}")
            for required in ("kind", "segment_index", "text_start", "text_end", "quote"):
                if required not in reference:
                    add("missing_required_field", f"{reference_path}:{required}")
            quote = reference.get("quote")
            if isinstance(quote, str) and not quote:
                add("empty_quote", reference_path)
            elif not isinstance(quote, str):
                add("invalid_quote_text", reference_path)
            elif len(quote) > 2000:
                add("invalid_quote_text", reference_path)
            segment_index = reference.get("segment_index")
            if type(segment_index) is not int or not 0 <= segment_index < len(
                case.student_segments
            ):
                add("invalid_quote_offsets", reference_path)
                source_text = None
            else:
                source_text = case.student_segments[segment_index].text
            text_start = reference.get("text_start")
            text_end = reference.get("text_end")
            coordinates_valid = type(text_start) is int and type(text_end) is int
            span_length: int | None = None
            if not coordinates_valid:
                if "text_start" in reference or "text_end" in reference:
                    add("invalid_quote_offsets", reference_path)
            else:
                span_length = text_end - text_start
                if text_start < 0 or text_end <= text_start or (
                    source_text is not None and text_end > len(source_text)
                ):
                    add("invalid_quote_offsets", reference_path)
                if isinstance(quote, str) and quote and span_length != len(quote):
                    add("invalid_quote_length", reference_path)
            source_matches = 0
            matches_at_offsets: bool | None = None
            if isinstance(quote, str) and quote and source_text is not None:
                source_matches = _count_overlapping_occurrences(source_text, quote)
                if coordinates_valid and 0 <= text_start < text_end <= len(source_text):
                    matches_at_offsets = source_text[text_start:text_end] == quote
                    if not matches_at_offsets and span_length == len(quote):
                        add(
                            "invalid_quote_offsets" if source_matches else "invalid_quote_text",
                            reference_path,
                        )
                elif source_matches == 0:
                    add("invalid_quote_text", reference_path)
            quote_checks.append(
                {
                    "criterion_index": criterion_index,
                    "evidence_index": reference_index,
                    "segment_index": segment_index if type(segment_index) is int else None,
                    "quote_codepoints": len(quote) if isinstance(quote, str) else None,
                    "claimed_span_codepoints": span_length,
                    "source_exact_match_count": source_matches if source_text is not None else None,
                    "matches_claimed_offsets": matches_at_offsets,
                }
            )

    if not counts:
        add("other_protocol_error", "unclassified_validation_failure")
    return {"category_occurrences": counts, "quote_checks": quote_checks}


def _count_overlapping_occurrences(text: str, needle: str) -> int:
    if not needle:
        return 0
    count = start = 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            return count
        count += 1
        start = found + 1


def _quote_match_summary(case_diagnostics: list[dict[str, object]]) -> dict[str, int]:
    checks = [
        item
        for case in case_diagnostics
        for item in case["quote_checks"]
    ]
    return {
        "quote_references_checked": len(checks),
        "exact_at_claimed_offsets": sum(item["matches_claimed_offsets"] is True for item in checks),
        "exact_text_present_in_claimed_segment": sum(
            item["source_exact_match_count"] is not None
            and item["source_exact_match_count"] > 0
            for item in checks
        ),
        "exact_text_found_elsewhere": sum(
            item["source_exact_match_count"] is not None
            and item["source_exact_match_count"] > 0
            and item["matches_claimed_offsets"] is False
            for item in checks
        ),
        "exact_text_not_found_in_claimed_segment": sum(
            item["source_exact_match_count"] == 0 for item in checks
        ),
        "claimed_offsets_uncheckable": sum(
            item["matches_claimed_offsets"] is None for item in checks
        ),
        "source_segment_unavailable": sum(
            item["source_exact_match_count"] is None for item in checks
        ),
    }


def _semantic_score_risk_cases(
    dataset: RubricEvaluationDataset,
    predictions_by_id: dict[str, EvaluationPredictionCase],
) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for case in dataset.cases:
        prediction = predictions_by_id[case.case_id]
        if case.reference_status != "included" or prediction.response_content is None:
            continue
        try:
            raw = json.loads(prediction.response_content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(raw, dict) or raw.get("outcome") != "candidate":
            continue
        scores = raw.get("criterion_scores")
        if not isinstance(scores, list):
            continue
        reference_by_key = {item.criterion_key: item for item in case.reference_criterion_scores}
        criterion_by_id = {
            str(_criterion_uuid(case, criterion.criterion_key)): criterion.criterion_key
            for criterion in case.rubric
        }
        mismatches: list[dict[str, str]] = []
        for score in scores:
            if not isinstance(score, dict):
                continue
            try:
                awarded = Decimal(str(score.get("awarded_points")))
            except (InvalidOperation, ValueError, TypeError):
                continue
            awarded_text = _bounded_decimal_text(awarded)
            if awarded_text is None:
                continue
            criterion_key = criterion_by_id.get(str(score.get("criterion_id")))
            reference = reference_by_key.get(criterion_key) if criterion_key else None
            if reference is not None and awarded != reference.awarded_points:
                mismatches.append(
                    {
                        "criterion_key": criterion_key,
                        "reference_points": _decimal_text(reference.awarded_points),
                        "unvalidated_response_points": awarded_text,
                    }
                )
        if not mismatches:
            continue
        try:
            raw_total = Decimal(str(raw.get("total_score")))
            raw_total_text = _bounded_decimal_text(raw_total)
        except (InvalidOperation, ValueError, TypeError):
            raw_total_text = None
        cases.append(
            {
                "case_id": case.case_id,
                "stored_status": prediction.status,
                "reference_total_score": _decimal_text(case.reference_total_score),
                "unvalidated_response_total_score": raw_total_text,
                "criterion_score_differences": mismatches,
                "included_in_candidate_score_metrics": prediction.status == "candidate",
                "human_semantic_review_required": True,
            }
        )
    return {
        "case_count": len(cases),
        "interpretation": (
            "这些是未经完整P3-B验证的原始候选输出与合成参考标签的"
            "数值差异，不是自动判定的教学错误。"
        ),
        "cases": cases,
        "rationales_and_quotes_included": False,
    }


def _bounded_decimal_text(value: Decimal) -> str | None:
    parts = value.as_tuple()
    if (
        not value.is_finite()
        or len(parts.digits) > 32
        or abs(parts.exponent) > 12
        or abs(value.adjusted()) > 20
    ):
        return None
    try:
        return _decimal_text(value)
    except InvalidOperation:
        return None


def _derive_prediction(
    case: RubricEvaluationCase,
    prediction: EvaluationPredictionCase,
    config: Settings,
) -> tuple[str, GradeModelReply | None, str | None]:
    if prediction.status == "not_requested":
        if prediction.error_code is not None:
            raise ValueError("not_requested_prediction_cannot_have_error_code")
        return "not_requested", None, None
    if prediction.response_content is None:
        if prediction.status not in {"provider_timeout", "provider_error", "input_too_large"}:
            raise ValueError("failed_prediction_missing_response_content")
        return prediction.status, None, prediction.error_code
    ended_reason = finish_reason_error(
        prediction.finish_reason,
        required=prediction.error_code == "provider_finish_reason_missing",
    )
    if ended_reason is not None:
        _require_status_match(prediction.status, "provider_error")
        if prediction.error_code != ended_reason:
            raise ValueError("prediction_finish_reason_error_mismatch")
        return "provider_error", None, ended_reason
    try:
        reply = GradeModelReply.model_validate_json(prediction.response_content)
    except ValidationError as error:
        classified = _classify_reply_validation_error(error)
        status = "protocol_error"
        _require_status_match(prediction.status, status)
        if prediction.error_code != classified.reason_code:
            raise ValueError("prediction_error_code_does_not_match_revalidated_response")
        return status, None, classified.reason_code
    if reply.outcome == "abstain":
        _require_status_match(prediction.status, "abstain")
        if prediction.error_code is not None:
            raise ValueError("abstention_cannot_have_error_code")
        return "abstain", reply, None
    try:
        _validate_candidate_scores(reply, build_grade_input(case), config)
    except GradeProposalError as error:
        status = _status_for_error(error)
        _require_status_match(prediction.status, status)
        if prediction.error_code != error.reason_code:
            raise ValueError("prediction_error_code_does_not_match_revalidated_response")
        return status, reply, error.reason_code
    _require_status_match(prediction.status, "candidate")
    if prediction.error_code is not None:
        raise ValueError("candidate_cannot_have_error_code")
    return "candidate", reply, None


def _status_for_error(error: GradeProposalError) -> str:
    if isinstance(error, GradeProposalInvalidEvidenceError):
        return "invalid_evidence"
    if isinstance(error, GradeProposalInvalidRubricScoresError):
        return "invalid_rubric_scores"
    if isinstance(error, GradeProposalInvalidModelResponseError):
        return "protocol_error"
    if error.status == "input_too_large":
        return "input_too_large"
    if error.status == "provider_timeout":
        return "provider_timeout"
    if error.status.startswith("provider_"):
        return "provider_error"
    return "protocol_error"


def _status_for_model_call_error(error: GradeProposalError) -> str:
    if error.reason_code in {
        "provider_response_truncated", "provider_generation_incomplete",
        "provider_finish_reason_missing",
    }:
        return "provider_error"
    if error.reason_code in {
        "model_evidence_protocol_invalid",
        "model_score_protocol_invalid",
        "model_json_contract_invalid",
    }:
        return "protocol_error"
    if error.reason_code == "provider_response_envelope_invalid":
        return "provider_error"
    return _status_for_error(error)


def _require_status_match(stored: str, derived: str) -> None:
    if stored != derived:
        raise ValueError("prediction_status_does_not_match_revalidated_response")


def _criterion_uuid(case: RubricEvaluationCase, criterion_key: str) -> UUID:
    return uuid5(_UUID_NAMESPACE, f"{case.case_id}:criterion:{criterion_key}")


def _reply_total(reply: GradeModelReply) -> Decimal:
    return sum((item.awarded_points for item in reply.criterion_scores), Decimal("0.00"))


def _count_quote_evidence(
    reply: GradeModelReply,
    case: RubricEvaluationCase,
    config: Settings,
    counts: Counter[str],
) -> None:
    if reply.outcome != "candidate":
        return
    document = build_grade_input(case)
    for score in reply.criterion_scores:
        if score.awarded_points > 0:
            counts["positive"] += 1
        quote_count = 0
        quote_valid = False
        for evidence in score.evidence_references:
            if not isinstance(evidence, QuoteEvidenceReference):
                continue
            quote_count += 1
            counts["checked"] += 1
            try:
                _validate_quote_reference(evidence, document, config)
            except GradeProposalInvalidEvidenceError:
                counts["invalid"] += 1
            else:
                counts["valid"] += 1
                quote_valid = True
        if score.awarded_points > 0 and quote_valid:
            counts["positive_with_quote"] += 1
        if score.awarded_points > 0 and not quote_valid and quote_count == 0:
            counts["positive_without_quote"] += 1


def _evidence_overlap(
    case: RubricEvaluationCase,
    reply: GradeModelReply,
) -> tuple[int, int, int]:
    if reply.outcome != "candidate":
        return 0, 0, 0
    gold: dict[tuple[str, int], list[tuple[int, int]]] = {}
    predicted: dict[tuple[str, int], list[tuple[int, int]]] = {}
    reference_by_key = {item.criterion_key: item for item in case.reference_criterion_scores}
    criterion_by_id = {
        _criterion_uuid(case, item.criterion_key): item.criterion_key for item in case.rubric
    }
    for criterion_key, reference in reference_by_key.items():
        for evidence in reference.evidence:
            segment_text = case.student_segments[evidence.segment_index].text
            start = (
                evidence.text_start
                if evidence.text_start is not None
                else segment_text.index(evidence.quote)
            )
            gold.setdefault((criterion_key, evidence.segment_index), []).append(
                (start, start + len(evidence.quote))
            )
    for score in reply.criterion_scores:
        criterion_key = criterion_by_id[score.criterion_id]
        for evidence in score.evidence_references:
            if isinstance(evidence, QuoteEvidenceReference):
                predicted.setdefault((criterion_key, evidence.segment_index), []).append(
                    (evidence.text_start, evidence.text_end)
                )
    keys = set(gold) | set(predicted)
    gold_chars = predicted_chars = intersection_chars = 0
    for key in keys:
        gold_ranges = _merge_ranges(gold.get(key, []))
        predicted_ranges = _merge_ranges(predicted.get(key, []))
        gold_chars += sum(end - start for start, end in gold_ranges)
        predicted_chars += sum(end - start for start, end in predicted_ranges)
        intersection_chars += _intersection_length(gold_ranges, predicted_ranges)
    return gold_chars, predicted_chars, intersection_chars


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            prior_start, prior_end = merged[-1]
            merged[-1] = (prior_start, max(prior_end, end))
    return merged


def _intersection_length(
    left: list[tuple[int, int]], right: list[tuple[int, int]]
) -> int:
    total = 0
    left_index = right_index = 0
    while left_index < len(left) and right_index < len(right):
        left_start, left_end = left[left_index]
        right_start, right_end = right[right_index]
        total += max(0, min(left_end, right_end) - max(left_start, right_start))
        if left_end <= right_end:
            left_index += 1
        else:
            right_index += 1
    return total


def _score_failure_categories(
    *, exact: bool, within: bool, criterion_error: bool, over: bool, under: bool
) -> list[str]:
    categories: list[str] = []
    if not exact:
        categories.append("total_score_within_tolerance" if within else "total_score_error")
        if over:
            categories.append("total_over_scored")
        if under:
            categories.append("total_under_scored")
    if criterion_error:
        categories.append("criterion_score_error")
        if exact:
            categories.append("offsetting_criterion_errors")
    return categories


def _failure_category(status: str) -> str:
    if status == "not_requested":
        return "not_requested"
    if status == "abstain":
        return "model_abstained_on_scored_reference"
    return status


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _runtime_detail(prediction: EvaluationPredictionCase) -> dict[str, int | None]:
    return {
        "elapsed_ms": prediction.elapsed_ms,
        "prompt_tokens": prediction.prompt_tokens,
        "completion_tokens": prediction.completion_tokens,
    }


def _mean_text(total: Decimal, denominator: int) -> str | None:
    if denominator == 0:
        return None
    return format((total / Decimal(denominator)).quantize(Decimal("0.0001")), "f")


def _ratio_text(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    return format((Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001")), "f")


class _CaptureProvider:
    """Capture the exact P3-B response while preserving its provider contract."""

    def __init__(self, provider: TextGradingProvider) -> None:
        self.provider = provider
        self.provider_name = provider.provider_name
        self.model_id = provider.model_id
        self.generation_config_digest = provider.generation_config_digest
        self.response_content: str | None = None
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        self.elapsed_ms: int | None = None
        self.response_model_id: str | None = None
        self.finish_reason: str | None = None
        self.reasoning_tokens: int | None = None
        self.cache_hit_tokens: int | None = None
        self.cache_miss_tokens: int | None = None

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> TextGradingResponse:
        ended_error = None
        try:
            response = await self.provider.complete(
                system_prompt=system_prompt, user_prompt=user_prompt,
            )
        except GradeProviderEndedResponseError as error:
            response = error.response
            ended_error = error
        self.response_content = response.content
        self.prompt_tokens = response.usage.prompt_tokens
        self.completion_tokens = response.usage.completion_tokens
        self.elapsed_ms = response.elapsed_ms
        self.response_model_id = response.model_id
        self.finish_reason = response.finish_reason
        self.reasoning_tokens = response.usage.reasoning_tokens
        self.cache_hit_tokens = response.usage.cache_hit_tokens
        self.cache_miss_tokens = response.usage.cache_miss_tokens
        if ended_error is not None:
            raise ended_error
        return response


async def request_prediction(
    case: RubricEvaluationCase,
    provider: TextGradingProvider,
    config: Settings,
    *,
    prompt_version: str = PROMPT_VERSION,
) -> EvaluationPredictionCase:
    """Call the existing P3-B provider/prompt/schema/score validators once per case."""

    import time

    captured = _CaptureProvider(provider)
    started = time.perf_counter()

    def metadata(**values: object) -> dict[str, object]:
        values.setdefault("response_content", captured.response_content)
        values.setdefault("prompt_tokens", captured.prompt_tokens)
        values.setdefault("completion_tokens", captured.completion_tokens)
        for name in (
            "response_model_id", "finish_reason", "reasoning_tokens",
            "cache_hit_tokens", "cache_miss_tokens",
        ):
            values.setdefault(name, getattr(captured, name))
        values.setdefault(
            "elapsed_ms",
            captured.elapsed_ms
            if captured.elapsed_ms is not None
            else max(0, round((time.perf_counter() - started) * 1000)),
        )
        return values

    try:
        reply, _, usage, elapsed_ms = await _call_and_validate(
            captured,
            build_grade_input(case),
            config=config,
            prompt_version=prompt_version,
        )
        captured.prompt_tokens, captured.completion_tokens = usage
        captured.elapsed_ms = elapsed_ms
        if reply.outcome == "abstain":
            return EvaluationPredictionCase(case_id=case.case_id, **metadata(status="abstain"))
        try:
            _validate_candidate_scores(reply, build_grade_input(case), config)
        except GradeProposalError as error:
            return EvaluationPredictionCase(
                case_id=case.case_id,
                **metadata(status=_status_for_error(error), error_code=error.reason_code),
            )
        return EvaluationPredictionCase(case_id=case.case_id, **metadata(status="candidate"))
    except GradeProposalError as error:
        return EvaluationPredictionCase(
            case_id=case.case_id,
            **metadata(status=_status_for_model_call_error(error), error_code=error.reason_code),
        )


def create_predictions_document(
    dataset: RubricEvaluationDataset,
    dataset_sha256: str,
    provider: TextGradingProvider,
    config: Settings,
    cases: list[EvaluationPredictionCase],
    *,
    prompt_version: str = PROMPT_VERSION,
) -> RubricEvaluationPredictions:
    return RubricEvaluationPredictions(
        dataset_version=dataset.dataset_version,
        dataset_sha256=dataset_sha256,
        provider=provider.provider_name,
        model_id=provider.model_id,
        prompt_version=prompt_version,
        generation_config_digest=provider.generation_config_digest,
        thinking_mode=(
            config.grading_deepseek_thinking
            if is_deepseek_endpoint(config.grading_base_url) else None
        ),
        reasoning_effort=(
            config.grading_deepseek_reasoning_effort
            if is_deepseek_endpoint(config.grading_base_url)
            and config.grading_deepseek_thinking == "enabled" else None
        ),
        configured_reasoning_effort=(
            config.grading_deepseek_reasoning_effort
            if is_deepseek_endpoint(config.grading_base_url) else None
        ),
        max_output_tokens=config.grading_max_output_tokens,
        invalid_response_retries=config.grading_invalid_response_retries,
        timeout_seconds=config.grading_timeout_seconds,
        created_at=datetime.now(UTC),
        cases=cases,
    )
