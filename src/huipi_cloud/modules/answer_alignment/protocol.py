"""Versioned, source-traceable answer-alignment protocol."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from huipi_cloud.modules.canonical_documents.protocol import CanonicalAssetReference

ALIGNMENT_SCHEMA_NAME = "huipi.answer.alignment"
ALIGNMENT_SCHEMA_VERSION = "1.0"
ALIGNER_VERSION = "1.0.0"

AlignmentResultStatus = Literal["complete", "review_required"]
QuestionMatchStatus = Literal["aligned", "review_required", "not_observed"]


class SourceRegion(BaseModel):
    """One non-overlapping content slice that points back to Canonical JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_index: int = Field(ge=0)
    source_block_id: UUID
    reading_order: int = Field(ge=0)
    bbox: tuple[float, float, float, float] | None = None
    source_type: str = Field(min_length=1, max_length=64)
    normalized_type: str = Field(min_length=1, max_length=32)
    content_pointer: str = Field(min_length=1, max_length=4096)
    text_start: int | None = Field(
        default=None,
        ge=0,
        description="Canonical 节点字符串中的 Unicode 码点起始偏移，包含起点。",
    )
    text_end: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Canonical 节点字符串中的 Unicode 码点结束偏移，不包含终点；"
            "遵循 Python 字符串切片语义。"
        ),
    )
    text: str | None = None
    asset_refs: list[CanonicalAssetReference] = Field(default_factory=list)


class QuestionEvidence(BaseModel):
    """A detected question-number marker and the reasons for its decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(min_length=1, max_length=32)
    question_number: int = Field(gt=0)
    question_id: UUID | None = None
    marker_kind: Literal[
        "explicit_chinese",
        "arabic_dot",
        "chinese_comma",
        "arabic_right_paren",
        "wrapped_parentheses",
    ]
    marker_text: str = Field(min_length=1, max_length=64)
    page_index: int = Field(ge=0)
    source_block_id: UUID
    reading_order: int = Field(ge=0)
    content_pointer: str = Field(min_length=1, max_length=4096)
    text_start: int = Field(
        ge=0,
        description="Canonical 节点字符串中的 Unicode 码点起始偏移，包含起点。",
    )
    text_end: int = Field(
        gt=0,
        description=(
            "Canonical 节点字符串中的 Unicode 码点结束偏移，不包含终点；"
            "遵循 Python 字符串切片语义。"
        ),
    )
    matching_status: Literal["aligned", "review_required", "unmatched"]
    reason_codes: list[str] = Field(default_factory=list)


class AlignedAnswer(BaseModel):
    """Student answer candidate for one real Assignment Question."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: UUID
    question_number: int = Field(gt=0)
    question_type: str = Field(min_length=1, max_length=30)
    matching_status: QuestionMatchStatus
    evidence: list[QuestionEvidence] = Field(default_factory=list)
    source_regions: list[SourceRegion] = Field(default_factory=list)
    text_projection: str = ""
    asset_refs: list[CanonicalAssetReference] = Field(default_factory=list)


class UnassignedRegion(BaseModel):
    """Document content that has no safe Question assignment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason_code: str = Field(min_length=1, max_length=64)
    evidence: QuestionEvidence | None = None
    source_regions: list[SourceRegion] = Field(default_factory=list)
    text_projection: str = ""


class AnswerAlignmentDocument(BaseModel):
    """Deterministic answer-to-question mapping for one immutable input tuple."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["huipi.answer.alignment"] = ALIGNMENT_SCHEMA_NAME
    schema_version: Literal["1.0"] = ALIGNMENT_SCHEMA_VERSION
    alignment_id: UUID
    submission_id: UUID
    assignment_id: UUID
    canonical_document_id: UUID
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aligner_version: str = Field(min_length=1, max_length=32)
    assignment_questions_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: AlignmentResultStatus
    candidates: list[QuestionEvidence] = Field(default_factory=list)
    answers: list[AlignedAnswer] = Field(default_factory=list)
    unassigned_regions: list[UnassignedRegion] = Field(default_factory=list)


class AlignmentSummaryRead(BaseModel):
    """Safe index summary. It never returns the private object key."""

    model_config = ConfigDict(extra="forbid")

    submission_id: UUID
    assignment_id: UUID
    status: Literal["not_generated", "complete", "review_required"]
    canonical_document_id: UUID | None = None
    aligner_version: str | None = None
    assignment_questions_digest: str | None = None
    question_count: int = Field(ge=0)
    aligned_count: int = Field(ge=0)
    review_required_count: int = Field(ge=0)
    unmatched_count: int = Field(ge=0)
    not_observed_count: int = Field(ge=0)


class QuestionAlignmentRead(BaseModel):
    """One question's answer candidate and traceable source slices."""

    model_config = ConfigDict(extra="forbid")

    submission_id: UUID
    assignment_id: UUID
    canonical_document_id: UUID
    answer: AlignedAnswer


class AlignmentErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str
    failure_code: str | None = None
