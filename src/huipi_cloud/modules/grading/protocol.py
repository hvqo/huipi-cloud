"""Versioned, source-bound text grading input documents."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

GRADE_INPUT_SCHEMA_NAME = "huipi.grading.input"
GRADE_INPUT_SCHEMA_VERSION = "1.0"

GradeInputStatus = Literal[
    "ready_text",
    "review_required",
    "source_stale",
    "unsupported_modality",
    "invalid_rubric",
    "source_unavailable",
    "size_limit_exceeded",
]


class GradeInputQuestionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: UUID
    question_number: int = Field(gt=0)
    question_type: str = Field(min_length=1, max_length=30)
    stem: str = Field(min_length=1)
    max_score: Decimal = Field(gt=0, max_digits=8, decimal_places=2)


class AnswerKeySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    answer_key_id: UUID
    question_id: UUID
    source: Literal["teacher", "model_generated"]
    answer_content: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RubricCriterionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: UUID
    question_id: UUID
    description: str = Field(min_length=1)
    points: Decimal = Field(gt=0, max_digits=8, decimal_places=2)
    sort_order: int = Field(gt=0)


class GradeInputAssetReference(BaseModel):
    """Safe asset identity copied from review evidence; never a storage locator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["stored", "external", "inline_redacted"]
    asset_id: UUID | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    content_type: str | None = Field(default=None, max_length=100)
    source_pointer: str | None = Field(default=None, max_length=4096)
    source_encoding: Literal["base64", "data_uri", "html_data_uri"] | None = None
    source_occurrence_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_reference_shape(self) -> Self:
        if self.kind == "stored" and self.asset_id is None:
            raise ValueError("stored references require an asset ID")
        if self.kind == "inline_redacted" and not all(
            (
                self.sha256,
                self.size_bytes is not None,
                self.content_type,
                self.source_pointer,
                self.source_encoding,
            )
        ):
            raise ValueError("inline references require redacted source metadata")
        return self


class SelectedAnswerSegment(BaseModel):
    """One exact reviewer-selected text slice with Unicode code-point offsets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_index: int = Field(ge=0)
    kind: Literal["text", "formula_text"]
    content_pointer: str = Field(min_length=1, max_length=4096)
    page_index: int = Field(ge=0)
    page_number: int = Field(ge=1)
    source_block_id: UUID
    reading_order: int = Field(ge=0)
    text_start: int = Field(ge=0)
    text_end: int = Field(gt=0)
    text: str = Field(min_length=1)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_slice(self) -> SelectedAnswerSegment:
        if self.text_end <= self.text_start:
            raise ValueError("text offsets must define a non-empty half-open range")
        if len(self.text) != self.text_end - self.text_start:
            raise ValueError("text offsets must use Unicode code points from the source node")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("segment checksum does not match the selected text")
        return self


class GradeInputEvidenceItem(BaseModel):
    """Audit record for each source region in the current human decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content_pointer: str = Field(min_length=1, max_length=4096)
    page_index: int = Field(ge=0)
    page_number: int = Field(ge=1)
    source_block_id: UUID
    reading_order: int = Field(ge=0)
    normalized_type: str = Field(min_length=1, max_length=32)
    text_start: int | None = Field(default=None, ge=0)
    text_end: int | None = Field(default=None, ge=0)
    representation: Literal["text", "formula_text", "unsupported"]
    reason_code: str | None = Field(default=None, max_length=64)
    asset_refs: list[GradeInputAssetReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence_shape(self) -> Self:
        if (self.text_start is None) != (self.text_end is None):
            raise ValueError("evidence offsets must be supplied as a pair")
        if self.text_start is not None and self.text_end is not None:
            if self.text_end <= self.text_start:
                raise ValueError("evidence offsets must define a non-empty half-open range")
        if (self.representation == "unsupported") != (self.reason_code is not None):
            raise ValueError("unsupported evidence requires a reason code")
        return self


class GradeInputCore(BaseModel):
    """Semantic scoring input, before storage identity and creation metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["huipi.grading.input"] = GRADE_INPUT_SCHEMA_NAME
    schema_version: Literal["1.0"] = GRADE_INPUT_SCHEMA_VERSION
    request_id: UUID
    submission_id: UUID
    assignment_id: UUID
    question: GradeInputQuestionSnapshot
    canonical_artifact_id: UUID
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_schema_version: str = Field(min_length=1, max_length=16)
    canonical_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alignment_artifact_id: UUID
    alignment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aligner_version: str = Field(min_length=1, max_length=32)
    assignment_questions_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_decision_id: UUID
    review_revision: int = Field(ge=1)
    review_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_schema_version: str = Field(min_length=1, max_length=16)
    review_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    excluded_prompt_regions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    answer_key: AnswerKeySnapshot
    rubric_snapshot: list[RubricCriterionSnapshot] = Field(min_length=1, max_length=100)
    rubric_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_answer_segments: list[SelectedAnswerSegment] = Field(min_length=1, max_length=100)
    evidence_manifest: list[GradeInputEvidenceItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_ready_text_payload(self) -> Self:
        if self.answer_key.question_id != self.question.question_id:
            raise ValueError("answer key snapshot must belong to the packaged Question")
        if hashlib.sha256(self.answer_key.answer_content.encode("utf-8")).hexdigest() != (
            self.answer_key.sha256
        ):
            raise ValueError("answer key snapshot checksum does not match")
        if any(item.question_id != self.question.question_id for item in self.rubric_snapshot):
            raise ValueError("rubric snapshots must belong to the packaged Question")
        if sum((item.points for item in self.rubric_snapshot), Decimal("0.00")) != (
            self.question.max_score
        ):
            raise ValueError("rubric snapshot total must equal the Question maximum score")
        if rubric_sha256(self.rubric_snapshot) != self.rubric_sha256:
            raise ValueError("rubric snapshot checksum does not match")
        if [item.segment_index for item in self.selected_answer_segments] != list(
            range(len(self.selected_answer_segments))
        ):
            raise ValueError("answer segments must use contiguous source-order indexes")
        if len(self.selected_answer_segments) != len(self.evidence_manifest):
            raise ValueError("every ready evidence region must have one text segment")
        for segment, evidence in zip(self.selected_answer_segments, self.evidence_manifest):
            if (
                evidence.representation != segment.kind
                or evidence.content_pointer != segment.content_pointer
                or evidence.page_index != segment.page_index
                or evidence.source_block_id != segment.source_block_id
                or evidence.reading_order != segment.reading_order
                or evidence.text_start != segment.text_start
                or evidence.text_end != segment.text_end
            ):
                raise ValueError("each answer segment must match its evidence manifest item")
        ranges: dict[str, list[tuple[int, int]]] = {}
        for segment in self.selected_answer_segments:
            for prior_start, prior_end in ranges.setdefault(segment.content_pointer, []):
                if max(segment.text_start, prior_start) < min(segment.text_end, prior_end):
                    raise ValueError("answer segments must not overlap within one Canonical node")
            ranges[segment.content_pointer].append((segment.text_start, segment.text_end))
        return self


class GradeInputDocument(GradeInputCore):
    """Immutable stored package with a semantic digest and storage timestamp."""

    artifact_id: UUID
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def validate_input_digest(self) -> Self:
        core = GradeInputCore.model_validate(
            self.model_dump(exclude={"artifact_id", "input_sha256", "created_at"})
        )
        if json_sha256(core) != self.input_sha256:
            raise ValueError("semantic input digest does not match")
        return self


class RubricInspectionItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: UUID
    sort_order: int
    points: Decimal
    description_sha256: str


class GradeInputInspection(BaseModel):
    """Safe CLI summary. It deliberately excludes answer and rubric text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: GradeInputStatus
    reason_codes: list[str] = Field(default_factory=list)
    submission_id: UUID
    assignment_id: UUID | None = None
    question_id: UUID
    question_number: int | None = None
    question_type: str | None = None
    max_score: Decimal | None = None
    canonical_artifact_id: UUID | None = None
    canonical_sha256: str | None = None
    alignment_artifact_id: UUID | None = None
    alignment_sha256: str | None = None
    review_decision_id: UUID | None = None
    review_revision: int | None = None
    answer_key_source: str | None = None
    answer_key_sha256: str | None = None
    rubric_sha256: str | None = None
    rubric_criterion_count: int = Field(default=0, ge=0)
    rubric_total_points: Decimal | None = None
    rubric: list[RubricInspectionItem] = Field(default_factory=list)
    selected_region_count: int = Field(default=0, ge=0)
    segment_count: int = Field(default=0, ge=0)
    selected_text_characters: int = Field(default=0, ge=0)
    evidence_manifest: list[GradeInputEvidenceItem] = Field(default_factory=list)
    input_sha256: str | None = None


class GradeInputPreparationRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    inspection: GradeInputInspection
    artifact_id: UUID | None = None
    input_sha256: str | None = None
    object_sha256: str | None = None
    created: bool = False


def canonical_json_bytes(value: object) -> bytes:
    """Encode JSON deterministically for digests and immutable S3 objects."""

    value = _json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def json_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def rubric_sha256(criteria: list[RubricCriterionSnapshot]) -> str:
    return json_sha256(criteria)
