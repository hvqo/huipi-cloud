"""Versioned, bounded embedding identity and numeric validation."""

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Protocol

MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
MODEL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
QUERY_INSTRUCTION = "Given a teacher question, retrieve relevant teaching reference passages"


class EmbeddingError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class EmbeddingIdentity:
    model_id: str = MODEL_ID
    model_revision: str = MODEL_REVISION
    dimension: int = 1024
    max_tokens: int = 512
    batch_size: int = 8
    query_instruction: str = QUERY_INSTRUCTION
    normalization: str = "l2-float32-v1"
    pooling: str = "last-token-v1"
    adapter_version: str = "local-qwen3-transformers-v1"

    def __post_init__(self):
        if (
            not 1 <= self.dimension <= 4096
            or not 1 <= self.batch_size <= 8
            or not 16 <= self.max_tokens <= 512
            or not self.model_revision
        ):
            raise ValueError("embedding_invalid_identity")

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def normalize(vector, dimension):
    if not isinstance(vector, (list, tuple)) or len(vector) != dimension:
        raise EmbeddingError("embedding_dimension_mismatch")
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
        raise EmbeddingError("embedding_non_finite_vector")
    length = math.hypot(*vector)
    if not math.isfinite(length) or length <= 1e-12:
        raise EmbeddingError("embedding_invalid_norm")
    return [float(x / length) for x in vector]


class EmbeddingProvider(Protocol):
    identity: EmbeddingIdentity

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...
    async def aclose(self) -> None: ...
