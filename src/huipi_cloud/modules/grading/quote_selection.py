"""Shared exact locator/converter plus frozen experimental prompts; no persistence."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from huipi_cloud.core.config import Settings
from huipi_cloud.modules.grading.prompts import SYSTEM_PROMPT, build_user_prompt
from huipi_cloud.modules.grading.proposal_errors import GradeProposalInvalidEvidenceError
from huipi_cloud.modules.grading.proposal_protocol import (
    CriterionScoreDraft,
    GradeModelReply,
    NoEvidenceReference,
    QuoteEvidenceReference,
)
from huipi_cloud.modules.grading.proposal_service import (
    _validate_candidate_scores,
    _validate_quote_reference,
)
from huipi_cloud.modules.grading.protocol import GradeInputDocument
from huipi_cloud.modules.grading.quote_semantics import (
    SEMANTIC_CHECKLIST,
    SEMANTIC_RULES,
    SEMANTICS_PROMPT_VERSION,
)

QUOTE_SELECTION_PROMPT_VERSION = "rubric-quote-selection-v1"
QUOTE_SELECTION_SCHEMA = "huipi.grading.quote-selection"
_OFFSET_INSTRUCTION = (
    "quote偏移使用对应segment文本中的Unicode码点半开区间[start,end)，"
    "且text_end必须等于text_start加quote的Unicode字符数。"
)
_SELECTION_INSTRUCTION = (
    "quote使用kind=quote_selection，只提供segment_index和逐字原文quote，"
    "不要计算或返回text_start、text_end、Canonical指针或来源ID。"
    "quote必须在指定片段内恰好出现一次；重复时引用足够长的原文以区分，无法区分则弃权。"
)
QUOTE_SELECTION_SYSTEM_PROMPT = SYSTEM_PROMPT.replace(_OFFSET_INSTRUCTION, _SELECTION_INSTRUCTION)


class QuoteSelectionReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["quote_selection"]
    segment_index: int = Field(ge=0, strict=True)
    quote: str = Field(min_length=1, max_length=2000, strict=True)


SelectionEvidence = Annotated[
    QuoteSelectionReference | NoEvidenceReference, Field(discriminator="kind")
]


class QuoteSelectionCriterionDraft(CriterionScoreDraft):
    evidence_references: list[SelectionEvidence] = Field(min_length=1, max_length=50)


class GradeQuoteSelectionReply(GradeModelReply):
    """Independent JSON identity; inherited score/outcome rules stay unchanged."""

    schema_name: Literal["huipi.grading.quote-selection"]
    criterion_scores: list[QuoteSelectionCriterionDraft] = Field(
        default_factory=list, max_length=100
    )


def build_quote_selection_prompt(document: GradeInputDocument) -> str:
    """Reuse all trusted input fields; change only the response contract."""

    payload = json.loads(build_user_prompt(document))
    for example_name in ("candidate_response_example", "abstention_response_example"):
        payload[example_name]["schema_name"] = QUOTE_SELECTION_SCHEMA
    example = payload["candidate_response_example"]["criterion_scores"][0]
    example["evidence_references"] = [
        {"kind": "quote_selection", "segment_index": 0, "quote": "原"}
    ]
    payload["response_shape_rule"] += "；quote_selection只提交片段索引和原文，不提交字符偏移"
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def quote_selection_prompts(
    document: GradeInputDocument, prompt_version: str = QUOTE_SELECTION_PROMPT_VERSION
) -> tuple[str, str]:
    """Select a frozen experimental prompt; v1 remains the default."""
    if prompt_version == QUOTE_SELECTION_PROMPT_VERSION:
        return QUOTE_SELECTION_SYSTEM_PROMPT, build_quote_selection_prompt(document)
    if prompt_version != SEMANTICS_PROMPT_VERSION:
        raise ValueError("unknown_quote_selection_prompt_version")
    payload = json.loads(build_quote_selection_prompt(document))
    payload["criterion_reasoning_checklist"] = SEMANTIC_CHECKLIST
    return (
        QUOTE_SELECTION_SYSTEM_PROMPT + "\n" + SEMANTIC_RULES,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )


def locate_quote(
    document: GradeInputDocument,
    segment_index: int,
    quote: str,
    config: Settings,
) -> QuoteEvidenceReference:
    """Locate one exact Unicode string, including overlapping duplicate matches."""

    if (
        not isinstance(segment_index, int)
        or isinstance(segment_index, bool)
        or segment_index < 0
        or segment_index >= len(document.selected_answer_segments)
    ):
        raise GradeProposalInvalidEvidenceError("quote_segment_missing")
    if (
        not isinstance(quote, str)
        or not quote.strip()
        or len(quote) > config.grading_max_quote_characters
    ):
        raise GradeProposalInvalidEvidenceError("quote_invalid")
    segment = document.selected_answer_segments[segment_index]
    if segment.segment_index != segment_index:
        raise GradeProposalInvalidEvidenceError("quote_source_mismatch")
    start = segment.text.find(quote)
    if start < 0:
        raise GradeProposalInvalidEvidenceError("quote_not_found")
    if segment.text.find(quote, start + 1) >= 0:
        raise GradeProposalInvalidEvidenceError("quote_ambiguous")
    evidence = QuoteEvidenceReference(
        kind="quote",
        segment_index=segment_index,
        text_start=start,
        text_end=start + len(quote),
        quote=quote,
    )
    try:
        _validate_quote_reference(evidence, document, config)
    except GradeProposalInvalidEvidenceError as error:
        raise GradeProposalInvalidEvidenceError("quote_source_mismatch") from error
    return evidence


def convert_quote_selections(
    reply: GradeQuoteSelectionReply,
    document: GradeInputDocument,
    config: Settings,
) -> GradeModelReply:
    """All references must convert; no partial repair or dropped references."""

    payload = reply.model_dump(mode="json", exclude={"schema_name"})
    for score, output in zip(reply.criterion_scores, payload["criterion_scores"], strict=True):
        output["evidence_references"] = [
            (
                locate_quote(document, reference.segment_index, reference.quote, config)
                if isinstance(reference, QuoteSelectionReference)
                else reference
            ).model_dump(mode="json")
            for reference in score.evidence_references
        ]
    return GradeModelReply.model_validate(payload)


def validate_quote_selection_candidate(
    reply: GradeQuoteSelectionReply,
    document: GradeInputDocument,
    config: Settings,
) -> GradeModelReply:
    converted = convert_quote_selections(reply, document, config)
    if converted.outcome == "candidate":
        _validate_candidate_scores(converted, document, config)
    return converted
