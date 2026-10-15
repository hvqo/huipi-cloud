"""Versioned source and citation contracts; no student/grade artifact ingestion."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SOURCE_BYTES = 64 * 1024
MAX_CHUNK_CHARS = 400
OVERLAP_CHARS = 48
MAX_CHUNKS = 256
MAX_SCOPE_CHUNKS = 4096
MAX_ACTIVE_DOCUMENTS = 32
CHUNKER_VERSION = "paragraph-unicode-v1"
RETRIEVER_VERSION = "unicode-weighted-coverage-v1"


class KnowledgeError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ImportRequest(Contract):
    assignment_id: UUID
    document_id: UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    source_type: Literal["markdown", "text"]
    permission: Literal["synthetic", "authorized"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    material_kind: Literal["teacher_reference"] = "teacher_reference"

    @model_validator(mode="after")
    def title_not_blank(self):
        if not self.title.strip():
            raise ValueError("title_blank")
        return self


class DocumentReceipt(Contract):
    document_id: UUID
    source_version: int
    source_sha256: str
    status: Literal["active", "superseded", "revoked"]
    chunk_count: int
    reused: bool


class RetrievalHit(Contract):
    citation_id: str = Field(pattern=r"^K[0-9a-f]{32}$")
    document_id: UUID
    title: str = Field(max_length=200)
    source_version: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_id: UUID
    chunk_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_start: int = Field(ge=0)
    text_end: int = Field(gt=0)
    content_pointer: Literal["/text"] = "/text"
    excerpt: str = Field(min_length=1, max_length=MAX_CHUNK_CHARS)
    relevance_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    truncated: bool = False

    @model_validator(mode="after")
    def offsets(self):
        if self.text_end - self.text_start != len(self.excerpt):
            raise ValueError("citation_unicode_range_mismatch")
        if self.citation_id != "K" + self.chunk_id.hex:
            raise ValueError("citation_identity_mismatch")
        return self


class SearchResult(Contract):
    hits: list[RetrievalHit] = Field(default_factory=list, max_length=5)
    truncated: bool = False
    retrieval_mode: Literal[
        "lexical", "dense", "hybrid", "dense_rerank", "hybrid_rerank", "lexical_rerank"
    ] = "lexical"
    requested_mode: (
        Literal["lexical", "dense", "hybrid", "dense_rerank", "hybrid_rerank", "lexical_rerank"]
        | None
    ) = None
    actual_mode: str | None = None
    degraded: bool = False
    degradation_reason: str | None = None
    embedding_model_version: str | None = None
    rerank_degraded: bool = False
    rerank_degradation_reason: str | None = None
    reranker_model_version: str | None = None
    candidate_count: int = Field(default=0, ge=0, le=20)
    rerank_elapsed_ms: int = Field(default=0, ge=0)
    rerank_input_tokens: int = Field(default=0, ge=0)
    index_status: Literal["not_used", "not_ready", "ready", "rebuilt"] = "not_used"
    retriever_version: str = RETRIEVER_VERSION
    score_meaning: str = "lexical_query_term_coverage_not_teaching_score_or_probability"


class CandidateResult(SearchResult):
    """Internal only; never sent wholesale to the Agent model."""

    hits: list[RetrievalHit] = Field(default_factory=list, max_length=20)
