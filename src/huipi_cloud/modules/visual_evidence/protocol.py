"""Strict versioned protocol for machine suggestions and provider replies."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

VISUAL_EVIDENCE_SCHEMA_NAME = "huipi.visual.evidence.proposal"
VISUAL_EVIDENCE_SCHEMA_VERSION = "1.0"
VISUAL_PROMPT_VERSION = "1.0.0"
VISUAL_PREPROCESSING_VERSION = "1.0.0"
VISUAL_COORDINATE_SPACE = "normalized_rendered_page_ratio_0_to_1"

Outcome = Literal["candidate_response_present", "candidate_prompt_only", "uncertain"]
EvidenceType = Literal[
    "handwritten_text",
    "handwritten_formula",
    "student_filled_table",
    "geometry_mark",
    "mixed_print_and_handwriting",
    "printed_prompt",
    "unclear",
]
ReasonCode = Literal[
    "handwriting_visible",
    "formula_marks_visible",
    "student_table_entries_visible",
    "geometry_marks_visible",
    "printed_prompt_only",
    "no_response_evidence_visible",
    "image_unclear",
    "question_attribution_uncertain",
    "shared_figure_possible",
    "source_mapping_unavailable",
    "visual_evidence_candidate_only",
]
VISUAL_REASON_CODES = frozenset(
    {
        "handwriting_visible",
        "formula_marks_visible",
        "student_table_entries_visible",
        "geometry_marks_visible",
        "printed_prompt_only",
        "no_response_evidence_visible",
        "image_unclear",
        "question_attribution_uncertain",
        "shared_figure_possible",
        "source_mapping_unavailable",
        "visual_evidence_candidate_only",
    }
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class VisualEvidenceModelRegion(StrictModel):
    """A raw model region. It cannot contain a Question ID or source pointer."""

    page_index: int = Field(ge=0, le=10000)
    visual_bbox: tuple[float, float, float, float]
    evidence_type: EvidenceType
    reason_codes: list[ReasonCode] = Field(default_factory=list, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def reject_coercible_or_non_finite_bbox(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        bbox = value.get("visual_bbox")
        if isinstance(bbox, (list, tuple)):
            if len(bbox) != 4 or any(
                isinstance(point, bool)
                or not isinstance(point, (int, float))
                or not math.isfinite(float(point))
                for point in bbox
            ):
                raise ValueError("visual_bbox must contain four finite numeric coordinates")
            value = {**value, "visual_bbox": tuple(float(point) for point in bbox)}
        return value

    @model_validator(mode="after")
    def validate_bbox(self) -> Self:
        x0, y0, x1, y1 = self.visual_bbox
        if (
            min(x0, y0, x1, y1) < 0
            or max(x0, y0, x1, y1) > 1
            or x1 <= x0
            or y1 <= y0
        ):
            raise ValueError("visual_bbox must be a non-empty normalized half-open box")
        evidence_reason = {
            "handwriting_visible",
            "formula_marks_visible",
            "student_table_entries_visible",
            "geometry_marks_visible",
        }
        if self.evidence_type == "printed_prompt" and evidence_reason.intersection(
            self.reason_codes
        ):
            raise ValueError("printed prompt region cannot claim student-work evidence")
        return self


class VisualEvidenceModelReply(StrictModel):
    """Provider JSON contract. A prompt-only result is not a negative review."""

    outcome: Outcome
    visual_regions: list[VisualEvidenceModelRegion] = Field(default_factory=list, max_length=24)
    reason_codes: list[ReasonCode] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def require_region_for_positive_or_prompt(self) -> Self:
        if self.outcome in {"candidate_response_present", "candidate_prompt_only"} and not (
            self.visual_regions
        ):
            raise ValueError("candidate outcomes require at least one visual region")
        if self.outcome == "candidate_prompt_only" and any(
            region.evidence_type != "printed_prompt" for region in self.visual_regions
        ):
            raise ValueError("candidate_prompt_only can only contain printed prompt regions")
        if self.outcome == "candidate_response_present" and not any(
            region.evidence_type
            in {
                "handwritten_text",
                "handwritten_formula",
                "student_filled_table",
                "geometry_mark",
                "mixed_print_and_handwriting",
            }
            for region in self.visual_regions
        ):
            raise ValueError("candidate response requires a non-prompt evidence region")
        return self


class VisualEvidenceRegion(StrictModel):
    """Raw image box plus an optional server-verified Canonical candidate."""

    page_index: int = Field(ge=0)
    visual_bbox: tuple[float, float, float, float]
    coordinate_space: Literal["normalized_rendered_page_ratio_0_to_1"] = (
        VISUAL_COORDINATE_SPACE
    )
    evidence_type: EvidenceType
    candidate_content_pointer: str | None = Field(default=None, max_length=4096)
    candidate_source_block_id: UUID | None = None
    attribution_status: Literal["candidate", "unresolved"]
    reason_codes: list[ReasonCode] = Field(default_factory=list, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def reject_invalid_candidate_bbox(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        bbox = value.get("visual_bbox")
        if isinstance(bbox, (list, tuple)):
            if len(bbox) != 4 or any(
                isinstance(point, bool)
                or not isinstance(point, (int, float))
                or not math.isfinite(float(point))
                for point in bbox
            ):
                raise ValueError("visual_bbox must contain four finite numeric coordinates")
            return {**value, "visual_bbox": tuple(float(point) for point in bbox)}
        return value

    @model_validator(mode="after")
    def validate_candidate_pair(self) -> Self:
        x0, y0, x1, y1 = self.visual_bbox
        if min(x0, y0, x1, y1) < 0 or max(x0, y0, x1, y1) > 1 or x1 <= x0 or y1 <= y0:
            raise ValueError("visual_bbox must be a non-empty normalized box")
        if (self.candidate_content_pointer is None) != (
            self.candidate_source_block_id is None
        ):
            raise ValueError("Canonical candidate pointer and block ID must appear together")
        if self.attribution_status == "candidate" and self.candidate_content_pointer is None:
            raise ValueError("candidate attribution requires a verified Canonical pointer")
        if self.attribution_status == "unresolved" and self.candidate_content_pointer is not None:
            raise ValueError("unresolved attribution cannot carry a Canonical pointer")
        return self


class RenderedPageMetadata(StrictModel):
    """Source and raster geometry retained with the proposal for audit."""

    page_index: int = Field(ge=0)
    source_width: float = Field(gt=0)
    source_height: float = Field(gt=0)
    source_unit: Literal["pdf_points", "image_pixels"]
    source_rotation_degrees: int = Field(ge=0, le=359)
    rendered_width: int = Field(gt=0)
    rendered_height: int = Field(gt=0)
    effective_dpi: float | None = Field(default=None, gt=0)
    exif_orientation: int | None = Field(default=None, ge=1, le=8)
    mapping_eligible: bool
    mapping_reason: str | None = Field(default=None, max_length=64)
    transform: tuple[float, float, float, float, float, float]
    rendered_image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProviderUsage(StrictModel):
    """Optional provider-reported token counts; absent means unavailable."""

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class VisualEvidenceProposal(StrictModel):
    """Immutable machine suggestion bound to all source and model versions."""

    schema_name: Literal["huipi.visual.evidence.proposal"] = VISUAL_EVIDENCE_SCHEMA_NAME
    schema_version: Literal["1.0"] = VISUAL_EVIDENCE_SCHEMA_VERSION
    proposal_id: UUID
    request_id: UUID
    submission_id: UUID
    assignment_id: UUID
    question_id: UUID
    question_number: int = Field(gt=0)
    canonical_artifact_id: UUID
    canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    alignment_artifact_id: UUID
    alignment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=32)
    preprocessing_version: str = Field(min_length=1, max_length=32)
    model_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_page_indexes: list[int] = Field(min_length=1, max_length=20)
    rendered_pages: list[RenderedPageMetadata] = Field(min_length=1, max_length=20)
    outcome: Outcome
    visual_regions: list[VisualEvidenceRegion] = Field(default_factory=list, max_length=24)
    reason_codes: list[ReasonCode] = Field(default_factory=list, max_length=12)
    model_elapsed_ms: int | None = Field(default=None, ge=0)
    provider_usage: ProviderUsage | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_page_references(self) -> Self:
        selected = set(self.selected_page_indexes)
        if len(selected) != len(self.selected_page_indexes):
            raise ValueError("selected page indexes must be unique")
        if any(index < 0 for index in selected):
            raise ValueError("selected page indexes must be non-negative")
        if {page.page_index for page in self.rendered_pages} != selected:
            raise ValueError("render metadata must exactly match selected pages")
        if any(region.page_index not in selected for region in self.visual_regions):
            raise ValueError("visual region refers to a page outside the submitted input")
        return self
