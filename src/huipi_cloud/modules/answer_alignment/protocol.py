"""Versioned, source-traceable answer-alignment protocol."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from huipi_cloud.modules.canonical_documents.protocol import CanonicalAssetReference

ALIGNMENT_SCHEMA_NAME = "huipi.answer.alignment"
ALIGNMENT_SCHEMA_VERSION = "1.0"
ALIGNER_VERSION = "1.1.0"

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
    matching_status: Literal["aligned", "review_required", "unmatched"] = Field(
        description=(
            "题号候选与 Question 的关联状态；aligned 只表示候选映射被接受，"
            "不表示学生作答存在或来源可评分。"
        )
    )
    reason_codes: list[str] = Field(default_factory=list)


class AlignedAnswer(BaseModel):
    """Question-mapped source regions; the type name does not confirm a response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: UUID
    question_number: int = Field(gt=0)
    question_type: str = Field(min_length=1, max_length=30)
    matching_status: QuestionMatchStatus = Field(
        description=(
            "来源区域与作业 Question 的关联状态。aligned 只表示该映射有足够证据；"
            "它不表示学生已作答，也不表示区域内部没有其他题目内容、来源不是印刷题干、"
            "OCR/公式/图像识别完整或内容可用于评分。"
            "not_observed 表示未找到可安全关联的题号，不表示学生未作答。"
        )
    )
    evidence: list[QuestionEvidence] = Field(default_factory=list)
    source_regions: list[SourceRegion] = Field(default_factory=list)
    text_projection: str = Field(
        default="",
        description="来源区域的文本投影；可能含题干或其他印刷内容，不保证是学生答案。",
    )
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
    status: AlignmentResultStatus = Field(
        description=(
            "文档对齐过程状态。complete 表示本轮对齐没有待复核或未分配区域；"
            "不表示学生已作答、区域内部没有其他题目内容、OCR 完整或结果可直接批改。"
        )
    )
    candidates: list[QuestionEvidence] = Field(default_factory=list)
    answers: list[AlignedAnswer] = Field(default_factory=list)
    unassigned_regions: list[UnassignedRegion] = Field(default_factory=list)


class AlignmentSummaryRead(BaseModel):
    """Safe index summary. It never returns the private object key."""

    model_config = ConfigDict(extra="forbid")

    submission_id: UUID
    assignment_id: UUID
    status: Literal["not_generated", "complete", "review_required"] = Field(
        description=(
            "答案来源对齐索引状态；complete 不是作答确认，也不是 grading-ready 状态。"
        )
    )
    canonical_document_id: UUID | None = None
    aligner_version: str | None = None
    assignment_questions_digest: str | None = None
    question_count: int = Field(ge=0)
    aligned_count: int = Field(
        ge=0,
        description=(
            "当前结果中 matching_status 为 aligned 的 Question 项数量；"
            "不是已确认学生作答数量，也不是可批改数量。"
        ),
    )
    review_required_count: int = Field(ge=0)
    unmatched_count: int = Field(ge=0)
    not_observed_count: int = Field(
        ge=0,
        description="未找到可安全关联来源的 Question 数量；不表示学生未作答。",
    )


class QuestionAlignmentRead(BaseModel):
    """One Question's mapped source slices and their alignment evidence."""

    model_config = ConfigDict(extra="forbid")

    submission_id: UUID
    assignment_id: UUID
    canonical_document_id: UUID
    answer: AlignedAnswer = Field(
        description=(
            "历史响应字段名。内容是 Question 对齐项与来源片段，不是已确认的学生答案；"
            "请按 matching_status 和 source_regions 读取。"
        )
    )


class AlignmentErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str
    failure_code: str | None = None
