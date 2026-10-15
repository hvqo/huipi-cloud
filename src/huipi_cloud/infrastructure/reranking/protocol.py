"""Bounded identity-preserving cross-encoder contract."""

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID

from huipi_cloud.modules.knowledge.schemas import RetrievalHit

MODEL_ID = "Qwen/Qwen3-Reranker-0.6B"
MODEL_REVISION = "e61197ed45024b0ed8a2d74b80b4d909f1255473"
INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"
MAX_CANDIDATES = 20


class RerankError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RerankIdentity:
    model_id: str = MODEL_ID
    model_revision: str = MODEL_REVISION
    hidden_size: int = 1024
    max_tokens: int = 1024
    batch_size: int = 4
    instruction: str = INSTRUCTION
    score_contract: str = "softmax-yes-no-last-token-float32-v1"
    adapter_version: str = "local-qwen3-rerank-v1"

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class RerankReply:
    scores: dict[UUID, float]
    model_id: str
    model_revision: str
    config_digest: str
    processed_count: int
    elapsed_ms: int
    input_tokens: int = 0
    cuda_allocated_bytes: int = 0
    cuda_peak_allocated_bytes: int = 0


def validate_candidates(query, candidates):
    if (
        not isinstance(query, str)
        or not 1 <= len(query.strip()) <= 250
        or not isinstance(candidates, list)
        or len(candidates) > MAX_CANDIDATES
    ):
        raise RerankError("rerank_input_limit")
    try:
        checked = [RetrievalHit.model_validate(c.model_dump()) for c in candidates]
    except (ValueError, AttributeError, TypeError):
        raise RerankError("rerank_invalid_candidate") from None
    if len({c.chunk_id for c in checked}) != len(checked):
        raise RerankError("rerank_duplicate_candidate")
    return checked


def validate_reply(reply, candidates, identity):
    ids = {c.chunk_id for c in candidates}
    if (
        not isinstance(reply, RerankReply)
        or reply.model_id != identity.model_id
        or reply.model_revision != identity.model_revision
        or reply.config_digest != identity.digest
    ):
        raise RerankError("rerank_model_mismatch")
    if (
        not isinstance(reply.scores, dict)
        or set(reply.scores) != ids
        or reply.processed_count != len(ids)
    ):
        raise RerankError("rerank_candidate_mismatch")
    if any(
        type(s) not in (float, int) or not math.isfinite(s) or not 0 <= s <= 1
        for s in reply.scores.values()
    ):
        raise RerankError("rerank_invalid_score")
    if (
        min(
            reply.elapsed_ms,
            reply.input_tokens,
            reply.cuda_allocated_bytes,
            reply.cuda_peak_allocated_bytes,
        )
        < 0
    ):
        raise RerankError("rerank_invalid_output")
    return reply


class RerankProvider(Protocol):
    identity: RerankIdentity

    async def rerank(self, query: str, candidates: list[RetrievalHit]) -> RerankReply: ...
    async def warmup(self) -> None: ...
    async def aclose(self) -> None: ...
