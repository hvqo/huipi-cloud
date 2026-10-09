"""Strict, MinerU-independent Canonical Document v1 data transfer objects."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

CANONICAL_SCHEMA_NAME = "huipi.canonical.document"
CANONICAL_SCHEMA_VERSION = "1.0"
NORMALIZER_VERSION = "1.0.0"
BBOX_COORDINATE_SPACE = "normalized_page_ratio_0_to_1"

NormalizedType = Literal[
    "text",
    "formula",
    "image",
    "table",
    "chart",
    "code",
    "list",
    "layout",
]


class CanonicalAssetReference(BaseModel):
    """Logical asset link that never contains a private object-storage key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["stored", "external", "inline_redacted"]
    asset_id: UUID | None = None
    uri: str | None = None
    sha256: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_reference_shape(self) -> Self:
        if self.kind == "stored" and (self.asset_id is None or self.uri is not None):
            raise ValueError("stored asset references require an asset_id and no URI")
        if self.kind == "external" and (self.uri is None or self.asset_id is not None):
            raise ValueError("external asset references require a URI and no asset_id")
        if self.kind == "inline_redacted" and (
            self.sha256 is None or self.size_bytes is None or self.uri is not None
        ):
            raise ValueError("redacted inline references require a digest and size")
        return self


class CanonicalAsset(BaseModel):
    """Non-secret manifest metadata for one stored image asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: UUID
    source_path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    content_type: str = Field(min_length=1, max_length=100)


class CanonicalContentNode(BaseModel):
    """One MinerU block/span in source order, with typed projection and trace data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    normalized_type: NormalizedType
    source_type: str = Field(min_length=1, max_length=64)
    source_index: int | None = Field(default=None, ge=0)
    bbox: tuple[float, float, float, float] | None = None
    content_kind: Literal["scalar", "children"]
    value: str | None = None
    children: list[CanonicalContentNode] = Field(default_factory=list)
    asset_refs: list[CanonicalAssetReference] = Field(default_factory=list)
    source_fields: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_content_shape(self) -> Self:
        if self.content_kind == "scalar" and (self.value is None or self.children):
            raise ValueError("scalar content requires a value and cannot have children")
        if self.content_kind == "children" and (self.value is not None):
            raise ValueError("child content cannot also have a scalar value")
        if self.bbox is not None:
            x0, y0, x1, y1 = self.bbox
            if (
                any(not 0.0 <= point <= 1.0 for point in self.bbox)
                or x1 <= x0
                or y1 <= y0
            ):
                raise ValueError("bbox must use MinerU normalized page coordinates")
        return self


class CanonicalBlock(BaseModel):
    """A top-level page block with stable identity and source location."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    block_id: UUID
    normalized_type: NormalizedType
    reading_order: int = Field(ge=0)
    page_index: int = Field(ge=0)
    page_number: int = Field(ge=1)
    source_page_idx: int = Field(ge=0)
    source_block_index: int = Field(ge=0)
    source_type: str = Field(min_length=1, max_length=64)
    bbox: tuple[float, float, float, float] | None = None
    content: CanonicalContentNode
    asset_refs: list[CanonicalAssetReference] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source_location(self) -> Self:
        if self.page_number != self.page_index + 1:
            raise ValueError("page_number must be one-based page_index")
        if self.source_page_idx != self.page_index:
            raise ValueError("canonical pages preserve the MinerU zero-based page index")
        if self.content.source_type != self.source_type:
            raise ValueError("block and content source types must agree")
        return self


class CanonicalPage(BaseModel):
    """One page in stable source order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_index: int = Field(ge=0)
    page_number: int = Field(ge=1)
    source_page_idx: int = Field(ge=0)
    blocks: list[CanonicalBlock]

    @model_validator(mode="after")
    def validate_source_location(self) -> Self:
        if self.page_number != self.page_index + 1 or self.source_page_idx != self.page_index:
            raise ValueError("canonical page numbering must preserve source page indexes")
        if any(
            block.page_index != self.page_index or block.page_number != self.page_number
            for block in self.blocks
        ):
            raise ValueError("canonical blocks must belong to their containing page")
        return self


class CanonicalDocument(BaseModel):
    """Stable, source-traceable document structure consumed by later modules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["huipi.canonical.document"] = CANONICAL_SCHEMA_NAME
    schema_version: Literal["1.0"] = CANONICAL_SCHEMA_VERSION
    document_id: UUID
    submission_id: UUID
    source_artifact_id: UUID
    source_parser: str = Field(min_length=1, max_length=32)
    source_parser_version: str = Field(min_length=1, max_length=64)
    source_schema_name: str = Field(min_length=1, max_length=64)
    source_schema_version: str = Field(min_length=1, max_length=16)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_middle_json_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_payload_schema_version: Literal["1"] = "1"
    bbox_coordinate_space: Literal["normalized_page_ratio_0_to_1"] = BBOX_COORDINATE_SPACE
    page_count: int = Field(ge=1)
    pages: list[CanonicalPage]
    assets: list[CanonicalAsset] = Field(default_factory=list)
    source_metadata: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_document_order_and_references(self) -> Self:
        if len(self.pages) != self.page_count:
            raise ValueError("page_count must match pages")
        if [page.page_index for page in self.pages] != list(range(self.page_count)):
            raise ValueError("pages must be ordered by contiguous zero-based index")
        reading_orders = [
            block.reading_order for page in self.pages for block in page.blocks
        ]
        if reading_orders != list(range(len(reading_orders))):
            raise ValueError("reading_order must be document-wide and contiguous")
        block_ids = [block.block_id for page in self.pages for block in page.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("block_id values must be unique")
        asset_ids = {asset.asset_id for asset in self.assets}
        for page in self.pages:
            for block in page.blocks:
                for reference in _walk_asset_references(block.content):
                    if reference.kind == "stored" and reference.asset_id not in asset_ids:
                        raise ValueError("stored references must resolve to a document asset")
        return self


def _walk_asset_references(
    node: CanonicalContentNode,
):
    yield from node.asset_refs
    for child in node.children:
        yield from _walk_asset_references(child)


class CanonicalDocumentSummary(BaseModel):
    """Safe metadata returned without the full canonical body or private object key."""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    source_artifact_id: UUID
    schema_version: str
    normalizer_version: str
    source_parser: str
    source_parser_version: str
    source_sha256: str
    canonical_sha256: str
    page_count: int
    block_count: int
    created_at: datetime


class CanonicalDocumentStatusRead(BaseModel):
    """Normalization status for one submission."""

    model_config = ConfigDict(extra="forbid")

    submission_id: UUID
    status: Literal["not_generated", "failed", "available"]
    source_parsing_status: str
    failure_code: str | None = None
    canonical_document: CanonicalDocumentSummary | None = None


class CanonicalPageRead(BaseModel):
    """One canonical page without neighboring pages."""

    model_config = ConfigDict(extra="forbid")

    document_id: UUID
    submission_id: UUID
    schema_version: str
    bbox_coordinate_space: str
    page: CanonicalPage


class CanonicalErrorResponse(BaseModel):
    """Stable public error body for canonical status and page endpoints."""

    model_config = ConfigDict(extra="forbid")

    detail: str
    failure_code: str | None = None
