"""Offline evaluation for synthetic rubric-grading references and predictions.

Reference labels live in a separate dataset document. Only the input fields are
copied into the in-memory GradeInputDocument sent to the existing P3-B prompt.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import TextGradingProvider, TextGradingResponse
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
        derived[case.case_id] = _derive_prediction(case, prediction, selected_config)

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
                "reason": reply.abstention_reason if reply is not None else None,
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
    return {
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

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> TextGradingResponse:
        response = await self.provider.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        self.response_content = response.content
        self.prompt_tokens = response.usage.prompt_tokens
        self.completion_tokens = response.usage.completion_tokens
        self.elapsed_ms = response.elapsed_ms
        return response


async def request_prediction(
    case: RubricEvaluationCase,
    provider: TextGradingProvider,
    config: Settings,
) -> EvaluationPredictionCase:
    """Call the existing P3-B provider/prompt/schema/score validators once per case."""

    import time

    captured = _CaptureProvider(provider)
    started = time.perf_counter()

    def metadata(**values: object) -> dict[str, object]:
        values.setdefault("response_content", captured.response_content)
        values.setdefault("prompt_tokens", captured.prompt_tokens)
        values.setdefault("completion_tokens", captured.completion_tokens)
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
) -> RubricEvaluationPredictions:
    return RubricEvaluationPredictions(
        dataset_version=dataset.dataset_version,
        dataset_sha256=dataset_sha256,
        provider=provider.provider_name,
        model_id=provider.model_id,
        prompt_version=PROMPT_VERSION,
        generation_config_digest=provider.generation_config_digest,
        invalid_response_retries=config.grading_invalid_response_retries,
        timeout_seconds=config.grading_timeout_seconds,
        created_at=datetime.now(UTC),
        cases=cases,
    )
