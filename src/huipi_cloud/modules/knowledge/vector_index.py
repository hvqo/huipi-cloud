"""Rebuildable process-local cache, never an authority for source permission or state."""

from array import array
from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID

from huipi_cloud.infrastructure.embedding.protocol import EmbeddingIdentity, normalize
from huipi_cloud.modules.knowledge.schemas import MAX_SCOPE_CHUNKS, KnowledgeError


@dataclass(frozen=True)
class VectorKey:
    assignment_id: UUID
    chunk_id: UUID
    model_id: str
    model_revision: str
    dimension: int
    embedding_config_digest: str
    chunk_text_sha256: str
    source_version: int

    @classmethod
    def for_chunk(cls, aid, doc, chunk, identity: EmbeddingIdentity):
        return cls(
            aid,
            chunk.id,
            identity.model_id,
            identity.model_revision,
            identity.dimension,
            identity.digest,
            chunk.chunk_text_sha256,
            doc.source_version,
        )


class MemoryVectorIndex:
    def __init__(self, capacity=MAX_SCOPE_CHUNKS):
        if not 1 <= capacity <= MAX_SCOPE_CHUNKS:
            raise ValueError("embedding_invalid_capacity")
        self.capacity, self.vectors = capacity, OrderedDict()

    @property
    def payload_bytes(self):
        return sum(len(v) * v.itemsize for v in self.vectors.values())

    def get(self, key):
        value = self.vectors.get(key)
        if value is not None:
            self.vectors.move_to_end(key)
        return value

    def publish(self, entries):
        if len(entries) > self.capacity:
            raise KnowledgeError("embedding_index_capacity_exceeded")
        # Validate all before mutating. No async point: readers cannot see half a batch.
        checked = {
            key: array("f", normalize(vector, key.dimension)) for key, vector in entries.items()
        }
        for key, vector in checked.items():
            self.vectors[key] = vector
            self.vectors.move_to_end(key)
        while len(self.vectors) > self.capacity:
            self.vectors.popitem(last=False)
