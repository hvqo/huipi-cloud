"""Versioned data contract for the synthetic human answer-presence baseline."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ANNOTATION_SCHEMA_NAME = "huipi.answer.presence.annotations"
ANNOTATION_SCHEMA_VERSION = "1.0"

AnnotationDecision = Literal["response_present", "response_absent", "uncertain"]
AnnotationRegionLabel = Literal[
    "printed_prompt",
    "student_response",
    "uncertain",
    "unlinked_response",
]


class SyntheticSourceNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1, max_length=64)
    content_pointer: str = Field(min_length=1, max_length=4096)
    page_index: int = Field(ge=0)
    block_id: str = Field(min_length=1, max_length=64)
    normalized_type: Literal["text", "formula", "image", "table", "layout"]
    text: str = Field(max_length=2000)
    has_image_asset: bool = False

    @model_validator(mode="after")
    def validate_canonical_pointer(self) -> SyntheticSourceNode:
        match = re.fullmatch(
            r"/pages/(\d+)/blocks/(\d+)/content(?:/children/\d+)*",
            self.content_pointer,
        )
        if match is None or int(match.group(1)) != self.page_index:
            raise ValueError("source pointer must be a valid Canonical node path on this page")
        return self


class SyntheticAnnotationRegion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1, max_length=64)
    label: AnnotationRegionLabel
    text_start: int | None = Field(default=None, ge=0, strict=True)
    text_end: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_range(self) -> SyntheticAnnotationRegion:
        if (self.text_start is None) != (self.text_end is None):
            raise ValueError("text offsets must be supplied as a pair")
        if self.text_start is not None and self.text_end is not None:
            if self.text_end <= self.text_start:
                raise ValueError("text offsets must be a non-empty half-open range")
        return self


class AnswerPresenceAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=64)
    scenario: str = Field(min_length=1, max_length=200)
    question_number: int | None = Field(default=None, gt=0)
    alignment_status: Literal["aligned", "review_required", "not_observed"]
    question_link: Literal["linked", "unlinked", "uncertain"]
    review_status: Literal["reviewed", "unreviewed"]
    decision: AnnotationDecision | None
    reason_codes: list[str] = Field(default_factory=list)
    source_nodes: list[SyntheticSourceNode] = Field(min_length=1)
    regions: list[SyntheticAnnotationRegion] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_annotation(self) -> AnswerPresenceAnnotation:
        if self.review_status == "unreviewed":
            if self.decision is not None or self.regions or self.reason_codes:
                raise ValueError("unreviewed scope cannot contain manual labels")
        elif self.decision is None:
            raise ValueError("reviewed scope requires a manual decision")

        nodes = {node.node_id: node for node in self.source_nodes}
        if len(nodes) != len(self.source_nodes):
            raise ValueError("source node identifiers must be unique within a case")
        pointers = [node.content_pointer for node in self.source_nodes]
        if len(pointers) != len(set(pointers)):
            raise ValueError("source pointers must be unique within a case")

        ranges: dict[str, list[tuple[int, int]]] = {}
        for region in self.regions:
            node = nodes.get(region.node_id)
            if node is None:
                raise ValueError("region must reference a declared source node")
            if region.text_start is None:
                if node.text:
                    raise ValueError("text nodes require explicit Unicode code-point offsets")
                if not node.has_image_asset and node.normalized_type != "image":
                    raise ValueError("whole-node selection requires image or empty layout evidence")
                continue
            assert region.text_end is not None
            if node.normalized_type == "image" or region.text_end > len(node.text):
                raise ValueError("region range exceeds its text node")
            ranges.setdefault(region.node_id, []).append((region.text_start, region.text_end))

        for node_ranges in ranges.values():
            node_ranges.sort()
            if any(
                current[0] < previous[1] for previous, current in zip(node_ranges, node_ranges[1:])
            ):
                raise ValueError("region annotations cannot overlap within one source node")
        labels = {region.label for region in self.regions}
        if self.decision == "response_present" and not labels.intersection(
            {"student_response", "unlinked_response"}
        ):
            raise ValueError("response_present requires a response region")
        if self.decision == "response_absent" and labels.intersection(
            {"student_response", "unlinked_response", "uncertain"}
        ):
            raise ValueError("response_absent cannot contain a response or uncertain region")
        if self.decision == "uncertain" and "uncertain" not in labels and not self.reason_codes:
            raise ValueError("uncertain labels require an uncertain region or reason")
        if (
            self.question_link == "unlinked"
            and "unlinked_response" not in labels
            and (self.decision == "response_present")
        ):
            raise ValueError("unlinked response must have a dedicated region label")
        return self


class AnswerPresenceAnnotationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["huipi.answer.presence.annotations"] = ANNOTATION_SCHEMA_NAME
    schema_version: Literal["1.0"] = ANNOTATION_SCHEMA_VERSION
    dataset_kind: Literal["synthetic_protocol_baseline"]
    cases: list[AnswerPresenceAnnotation] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_ids(self) -> AnswerPresenceAnnotationDataset:
        identifiers = [case.case_id for case in self.cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("case_id values must be unique")
        return self
