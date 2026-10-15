"""Reproducible paragraph-preferred chunks; offsets always refer to original Unicode text."""

import hashlib
import re
from dataclasses import dataclass

from huipi_cloud.modules.knowledge.schemas import (
    MAX_CHUNK_CHARS,
    MAX_CHUNKS,
    MAX_SOURCE_BYTES,
    OVERLAP_CHARS,
    KnowledgeError,
)


@dataclass(frozen=True)
class TextChunk:
    index: int
    start: int
    end: int
    text: str
    sha256: str


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def decode_source(raw: bytes, expected_sha: str) -> str:
    if not raw or len(raw) > MAX_SOURCE_BYTES:
        raise KnowledgeError("knowledge_source_size_limit")
    if sha256(raw) != expected_sha:
        raise KnowledgeError("knowledge_source_sha_mismatch")
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise KnowledgeError("knowledge_source_invalid_utf8") from None
    if not value.strip() or "\x00" in value:
        raise KnowledgeError("knowledge_source_invalid_text")
    return value


def chunk_text(value: str) -> list[TextChunk]:
    result, start = [], 0
    while start < len(value):
        end = min(start + MAX_CHUNK_CHARS, len(value))
        if end < len(value):
            # Prefer paragraph/header boundaries in the latter half, then line boundaries.
            segment = value[start + MAX_CHUNK_CHARS // 2 : end]
            breaks = list(re.finditer(r"\n\n|\n(?=#)", segment))
            if not breaks:
                breaks = list(re.finditer(r"\n", segment))
            if breaks:
                end = start + MAX_CHUNK_CHARS // 2 + breaks[-1].end()
        part = value[start:end]
        result.append(TextChunk(len(result), start, end, part, sha256(part.encode())))
        if len(result) > MAX_CHUNKS:
            raise KnowledgeError("knowledge_chunk_count_limit")
        if end == len(value):
            break
        start = end - OVERLAP_CHARS
    return result
