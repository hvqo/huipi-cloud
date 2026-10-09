"""Pure, bounded conversion from the MinerU 4.x MiddleJson wire format."""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid5

from pydantic import ValidationError

from huipi_cloud.modules.canonical_documents.errors import CanonicalNormalizationError
from huipi_cloud.modules.canonical_documents.protocol import (
    BBOX_COORDINATE_SPACE,
    CANONICAL_SCHEMA_NAME,
    CANONICAL_SCHEMA_VERSION,
    CanonicalAsset,
    CanonicalAssetReference,
    CanonicalBlock,
    CanonicalContentNode,
    CanonicalDocument,
    CanonicalPage,
)

_PAGE_TYPES = {
    "image",
    "table",
    "chart",
    "code",
    "text",
    "equation",
    "list",
    "index",
    "ref_text",
    "header",
    "footer",
    "page_number",
    "aside_text",
    "page_footnote",
    "doc_title",
    "paragraph_title",
}
_NESTED_TYPES = _PAGE_TYPES | {
    "image_body",
    "image_caption",
    "image_footnote",
    "table_body",
    "table_caption",
    "table_footnote",
    "chart_body",
    "chart_caption",
    "chart_footnote",
    "code_body",
    "algorithm_body",
    "code_caption",
    "code_footnote",
}
_SPAN_TYPES = {"text", "equation_inline", "code_inline", "hyperlink"}
_VISUAL_HTML_TYPES = {"image_body", "table_body", "chart_body"}
_IMAGE_CONTENT_TYPES = {
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/svg+xml",
    "image/tiff",
    "image/webp",
}
_SOURCE_FIELD_REDACTIONS = {
    "access_key",
    "secret_key",
    "secret_access_key",
    "canonical_object_key",
    "object_key",
    "storage_key",
    "bucket",
}


@dataclass(frozen=True)
class NormalizationSource:
    """Immutable metadata copied from one successful ParsedArtifact index."""

    document_id: UUID
    submission_id: UUID
    source_artifact_id: UUID
    source_parser: str
    source_parser_version: str
    source_schema_name: str
    source_schema_version: str
    source_sha256: str
    source_middle_json_sha256: str
    source_page_count: int
    source_asset_count: int
    source_tier: str


def normalize_middle_json(
    middle_json: bytes | dict[str, Any],
    assets_manifest: bytes | dict[str, Any],
    source: NormalizationSource,
    *,
    max_nodes: int = 50_000,
    max_nesting_depth: int = 64,
) -> CanonicalDocument:
    """Convert one validated P2-B result into deterministic Canonical Document v1."""
    if max_nodes < 1 or max_nesting_depth < 1:
        raise ValueError("normalization limits must be positive")
    middle = _load_json(middle_json)
    manifest = _load_json(assets_manifest)
    _validate_json_tree(middle, max_nodes=max_nodes, max_depth=max_nesting_depth)
    _validate_json_tree(manifest, max_nodes=max_nodes, max_depth=max_nesting_depth)
    _validate_source_identity(middle, manifest, source)

    manifest_assets = _parse_manifest(
        manifest,
        source.source_asset_count,
        source.source_artifact_id,
    )
    resolver = _AssetResolver(manifest_assets)
    pages_raw = middle["pages"]
    pages: list[CanonicalPage] = []
    document_order = 0
    node_counter = [0]

    for expected_page_index, raw_page in enumerate(pages_raw):
        page_index = raw_page.get("page_idx") if isinstance(raw_page, dict) else None
        if (
            not isinstance(raw_page, dict)
            or not isinstance(page_index, int)
            or isinstance(page_index, bool)
            or page_index != expected_page_index
        ):
            raise CanonicalNormalizationError("invalid_source_structure")
        raw_blocks = raw_page.get("blocks")
        if not isinstance(raw_blocks, list):
            raise CanonicalNormalizationError("invalid_source_structure")

        blocks: list[CanonicalBlock] = []
        previous_source_index = -1
        for raw_block in raw_blocks:
            if not isinstance(raw_block, dict):
                raise CanonicalNormalizationError("invalid_source_structure")
            source_type = raw_block.get("type")
            source_index = raw_block.get("index")
            if (
                not isinstance(source_type, str)
                or source_type not in _PAGE_TYPES
                or not isinstance(source_index, int)
                or isinstance(source_index, bool)
                or source_index <= previous_source_index
                or "content" not in raw_block
            ):
                raise CanonicalNormalizationError("invalid_source_structure")
            previous_source_index = source_index
            content_node = _build_content_node(
                raw_block,
                resolver,
                node_counter,
                max_nodes=max_nodes,
                max_depth=max_nesting_depth,
                depth=0,
            )
            references = list(_walk_references(content_node))
            blocks.append(
                CanonicalBlock(
                    block_id=uuid5(
                        source.document_id,
                        f"page:{expected_page_index}:block:{source_index}",
                    ),
                    normalized_type=_normalized_type(source_type),
                    reading_order=document_order,
                    page_index=expected_page_index,
                    page_number=expected_page_index + 1,
                    source_page_idx=expected_page_index,
                    source_block_index=source_index,
                    source_type=source_type,
                    bbox=_parse_bbox(raw_block.get("bbox")),
                    content=content_node,
                    asset_refs=references,
                )
            )
            document_order += 1
        pages.append(
            CanonicalPage(
                page_index=expected_page_index,
                page_number=expected_page_index + 1,
                source_page_idx=expected_page_index,
                blocks=blocks,
            )
        )

    try:
        return CanonicalDocument(
            schema_name=CANONICAL_SCHEMA_NAME,
            schema_version=CANONICAL_SCHEMA_VERSION,
            document_id=source.document_id,
            submission_id=source.submission_id,
            source_artifact_id=source.source_artifact_id,
            source_parser=source.source_parser,
            source_parser_version=source.source_parser_version,
            source_schema_name=source.source_schema_name,
            source_schema_version=source.source_schema_version,
            source_sha256=source.source_sha256,
            source_middle_json_sha256=source.source_middle_json_sha256,
            source_payload_schema_version="1",
            bbox_coordinate_space=BBOX_COORDINATE_SPACE,
            page_count=len(pages),
            pages=pages,
            assets=sorted(resolver.assets.values(), key=lambda item: item.source_path),
            source_metadata={
                "metadata": _safe_source_value(middle["metadata"]),
                "extensions": _safe_source_value(middle["extensions"]),
                "is_full_document": middle["is_full_document"],
            },
        )
    except ValidationError as error:
        raise CanonicalNormalizationError("invalid_source_structure") from error


def _load_json(value: bytes | dict[str, Any]) -> object:
    if isinstance(value, dict):
        return value
    if not isinstance(value, bytes):
        raise CanonicalNormalizationError("invalid_source_structure")
    try:
        return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError) as error:
        raise CanonicalNormalizationError("invalid_source_json") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _validate_json_tree(value: object, *, max_nodes: int, max_depth: int) -> None:
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > max_nodes or depth > max_depth:
            raise CanonicalNormalizationError("normalization_limit_exceeded")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise CanonicalNormalizationError("invalid_source_structure")
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise CanonicalNormalizationError("invalid_source_structure")
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise CanonicalNormalizationError("invalid_source_structure")


def _validate_source_identity(
    middle: object,
    manifest: object,
    source: NormalizationSource,
) -> None:
    if not isinstance(middle, dict) or not isinstance(manifest, dict):
        raise CanonicalNormalizationError("invalid_source_structure")
    metadata = middle.get("metadata")
    extensions = middle.get("extensions")
    pages = middle.get("pages")
    if not isinstance(metadata, dict) or not isinstance(extensions, dict):
        raise CanonicalNormalizationError("invalid_source_structure")
    producer = metadata.get("producer")
    mineru = extensions.get("mineru")
    if (
        middle.get("schema") != source.source_schema_name
        or middle.get("schema") != "docvortex.middle"
        or middle.get("schema_version") != source.source_schema_version
        or middle.get("schema_version") != "2.0"
        or not isinstance(source.source_parser_version, str)
        or not isinstance(source.source_schema_name, str)
        or not isinstance(source.source_schema_version, str)
        or source.source_parser != "MinerU"
        or not re.fullmatch(r"4\.\d+\.\d+", source.source_parser_version)
        or metadata.get("file_suffix") != "pdf"
        or not isinstance(metadata.get("document"), (dict, type(None)))
        or not isinstance(producer, dict)
        or producer.get("name") != "mineru"
        or producer.get("version") != source.source_parser_version
        or not isinstance(mineru, dict)
        or mineru.get("tier") != source.source_tier
        or mineru.get("parse_mode") not in {"txt", "ocr"}
        or not isinstance(middle.get("is_full_document"), bool)
        or not isinstance(pages, list)
        or not pages
        or len(pages) != source.source_page_count
        or not re.fullmatch(r"[0-9a-f]{64}", source.source_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", source.source_middle_json_sha256)
        or not isinstance(manifest.get("assets"), list)
        or manifest.get("schema") != "huipi.parsed-assets"
        or manifest.get("version") != "1"
        or len(manifest["assets"]) != source.source_asset_count
    ):
        raise CanonicalNormalizationError("unsupported_source_schema")


def _parse_manifest(
    manifest: object,
    expected_count: int,
    source_artifact_id: UUID,
) -> list[CanonicalAsset]:
    if not isinstance(manifest, dict):
        raise CanonicalNormalizationError("invalid_asset_manifest")
    records = manifest.get("assets")
    if not isinstance(records, list) or len(records) != expected_count:
        raise CanonicalNormalizationError("invalid_asset_manifest")
    assets: list[CanonicalAsset] = []
    seen_paths: set[str] = set()
    seen_keys: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise CanonicalNormalizationError("invalid_asset_manifest")
        path = record.get("path")
        key = record.get("object_key")
        digest = record.get("sha256")
        size = record.get("size_bytes")
        content_type = record.get("content_type")
        normalized_path = _normalize_relative_path(path, reject_percent=False)
        if (
            normalized_path is None
            or not isinstance(key, str)
            or not key.strip()
            or key in seen_keys
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or content_type not in _IMAGE_CONTENT_TYPES
            or normalized_path in seen_paths
        ):
            raise CanonicalNormalizationError("invalid_asset_manifest")
        seen_paths.add(normalized_path)
        seen_keys.add(key)
        assets.append(
            CanonicalAsset(
                asset_id=uuid5(
                    source_artifact_id,
                    f"asset:{normalized_path}:{digest}",
                ),
                source_path=normalized_path,
                sha256=digest,
                size_bytes=size,
                content_type=content_type,
            )
        )
    return sorted(assets, key=lambda asset: asset.source_path)


def _build_content_node(
    value: dict[str, Any],
    resolver: "_AssetResolver",
    node_counter: list[int],
    *,
    max_nodes: int,
    max_depth: int,
    depth: int,
) -> CanonicalContentNode:
    if depth > max_depth:
        raise CanonicalNormalizationError("normalization_limit_exceeded")
    node_counter[0] += 1
    if node_counter[0] > max_nodes:
        raise CanonicalNormalizationError("normalization_limit_exceeded")
    source_type = value.get("type")
    if not isinstance(source_type, str) or source_type not in (_NESTED_TYPES | _SPAN_TYPES):
        raise CanonicalNormalizationError("unsupported_source_block_type")
    if "index" in value and value["index"] is not None and (
        not isinstance(value["index"], int)
        or isinstance(value["index"], bool)
        or value["index"] < 0
    ):
        raise CanonicalNormalizationError("invalid_source_structure")
    if "content" not in value:
        raise CanonicalNormalizationError("invalid_source_structure")
    raw_content = value["content"]
    if isinstance(raw_content, str):
        content_kind = "scalar"
        scalar_value = _redact_inline_data_urls(raw_content)
        raw_children = []
    elif isinstance(raw_content, list):
        content_kind = "children"
        scalar_value = None
        raw_children = raw_content
    else:
        raise CanonicalNormalizationError("invalid_source_structure")

    references: list[CanonicalAssetReference] = []
    for key in ("image_path", "img_path", "image_source", "image_url"):
        reference = value.get(key)
        if reference is None:
            continue
        if not isinstance(reference, str):
            raise CanonicalNormalizationError("invalid_asset_reference")
        references.append(resolver.resolve(reference))
    if source_type in _VISUAL_HTML_TYPES and isinstance(raw_content, str):
        for reference in _html_image_references(raw_content):
            references.append(resolver.resolve(reference))

    children: list[CanonicalContentNode] = []
    for child in raw_children:
        if not isinstance(child, dict):
            raise CanonicalNormalizationError("invalid_source_structure")
        children.append(
            _build_content_node(
                child,
                resolver,
                node_counter,
                max_nodes=max_nodes,
                max_depth=max_depth,
                depth=depth + 1,
            )
        )

    try:
        return CanonicalContentNode(
            normalized_type=_normalized_type(source_type),
            source_type=source_type,
            source_index=value.get("index"),
            bbox=_parse_bbox(value.get("bbox")),
            content_kind=content_kind,
            value=scalar_value,
            children=children,
            asset_refs=references,
            source_fields={
                key: _safe_source_value(item, parent_key=key)
                for key, item in value.items()
                if key not in {"type", "index", "bbox", "content"}
            },
        )
    except ValidationError as error:
        raise CanonicalNormalizationError("invalid_source_structure") from error


class _ImageSourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._capture(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._capture(tag, attrs)

    def _capture(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "img":
            return
        for key, value in attrs:
            if key.casefold() == "src" and value is not None:
                self.references.append(value.strip())


def _html_image_references(value: str) -> list[str]:
    parser = _ImageSourceParser()
    try:
        parser.feed(value)
        parser.close()
    except ValueError as error:
        raise CanonicalNormalizationError("invalid_asset_reference") from error
    return parser.references


class _AssetResolver:
    def __init__(self, assets: list[CanonicalAsset]) -> None:
        self.by_path = {asset.source_path: asset for asset in assets}
        self.assets: dict[str, CanonicalAsset] = {}

    def resolve(self, reference: str) -> CanonicalAssetReference:
        if (
            not reference
            or len(reference.encode("utf-8")) > 4096
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in reference)
        ):
            raise CanonicalNormalizationError("invalid_asset_reference")
        try:
            parsed = urlsplit(reference)
        except ValueError as error:
            raise CanonicalNormalizationError("invalid_asset_reference") from error
        scheme = parsed.scheme.casefold()
        if scheme in {"http", "https"}:
            if not parsed.hostname or parsed.username or parsed.password:
                raise CanonicalNormalizationError("invalid_asset_reference")
            try:
                _ = parsed.port
            except ValueError as error:
                raise CanonicalNormalizationError("invalid_asset_reference") from error
            return CanonicalAssetReference(kind="external", uri=reference)
        if scheme == "data":
            if not reference.casefold().startswith("data:image/") or "," not in reference:
                raise CanonicalNormalizationError("invalid_asset_reference")
            encoded = reference.encode("utf-8")
            return CanonicalAssetReference(
                kind="inline_redacted",
                sha256=hashlib.sha256(encoded).hexdigest(),
                size_bytes=len(encoded),
            )
        if scheme or parsed.netloc or re.search(r"%(?![0-9a-fA-F]{2})", parsed.path):
            raise CanonicalNormalizationError("invalid_asset_reference")
        try:
            decoded_path = unquote(parsed.path, errors="strict")
        except UnicodeDecodeError as error:
            raise CanonicalNormalizationError("invalid_asset_reference") from error
        normalized_path = _normalize_relative_path(decoded_path, reject_percent=False)
        if normalized_path is None:
            raise CanonicalNormalizationError("invalid_asset_reference")
        candidates = [normalized_path]
        if not normalized_path.startswith("images/"):
            candidates.append(f"images/{normalized_path}")
        asset = next((self.by_path[path] for path in candidates if path in self.by_path), None)
        if asset is None:
            raise CanonicalNormalizationError("missing_asset_reference")
        self.assets[asset.source_path] = asset
        return CanonicalAssetReference(kind="stored", asset_id=asset.asset_id)


def _normalize_relative_path(value: object, *, reject_percent: bool) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    if reject_percent and re.search(r"%(?![0-9a-fA-F]{2})", value):
        return None
    if value.startswith("/") or "\\" in value:
        return None
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if path.is_absolute() or windows_path.is_absolute() or windows_path.drive or ".." in path.parts:
        return None
    normalized = "/".join(part for part in path.parts if part not in {"", "."})
    return normalized or None


def _parse_bbox(value: object) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise CanonicalNormalizationError("invalid_source_bbox")
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        raise CanonicalNormalizationError("invalid_source_bbox")
    try:
        coordinates = tuple(float(item) for item in value)
    except (OverflowError, ValueError) as error:
        raise CanonicalNormalizationError("invalid_source_bbox") from error
    if (
        any(not math.isfinite(item) or not 0 <= item <= 1 for item in coordinates)
        or coordinates[2] <= coordinates[0]
        or coordinates[3] <= coordinates[1]
    ):
        raise CanonicalNormalizationError("invalid_source_bbox")
    return coordinates


def _normalized_type(source_type: str) -> str:
    if source_type == "equation" or source_type == "equation_inline":
        return "formula"
    if source_type in {"image", "image_body", "image_caption", "image_footnote"}:
        return "image"
    if source_type in {"table", "table_body", "table_caption", "table_footnote"}:
        return "table"
    if source_type in {"chart", "chart_body", "chart_caption", "chart_footnote"}:
        return "chart"
    if source_type in {"code", "code_body", "code_caption", "code_footnote", "code_inline"}:
        return "code"
    if source_type == "list":
        return "list"
    if source_type in {"text", "hyperlink"}:
        return "text"
    if source_type in {
        "index",
        "ref_text",
        "header",
        "footer",
        "page_number",
        "aside_text",
        "page_footnote",
        "doc_title",
        "paragraph_title",
        "algorithm_body",
    }:
        return "layout"
    raise CanonicalNormalizationError("unsupported_source_block_type")


def _safe_source_value(value: object, *, parent_key: str = "") -> Any:
    if parent_key.casefold() == "image_base64":
        if not isinstance(value, str):
            raise CanonicalNormalizationError("invalid_source_structure")
        encoded = value.encode("utf-8")
        return {
            "_canonical_omitted": "inline_binary_asset",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
        }
    if parent_key.casefold() in _SOURCE_FIELD_REDACTIONS:
        return {"_canonical_omitted": "private_storage_location"}
    if isinstance(value, str):
        return _redact_inline_data_urls(value)
    if isinstance(value, dict):
        return {
            key: _safe_source_value(item, parent_key=key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_source_value(item) for item in value]
    return value


_INLINE_DATA_URL = re.compile(r"data:image/[^\s\"'<>)]*", flags=re.IGNORECASE)


def _redact_inline_data_urls(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        encoded = match.group(0).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        return f"canonical-inline-image:{digest}:{len(encoded)}"

    return _INLINE_DATA_URL.sub(replace, value)


def _walk_references(node: CanonicalContentNode):
    yield from node.asset_refs
    for child in node.children:
        yield from _walk_references(child)
