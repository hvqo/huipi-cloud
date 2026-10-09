"""Canonical Document v1 conversion, fidelity, determinism, and limit tests."""

import hashlib
import json
from uuid import uuid4, uuid5

import pytest

from huipi_cloud.modules.canonical_documents.errors import CanonicalNormalizationError
from huipi_cloud.modules.canonical_documents.normalizer import (
    NormalizationSource,
    normalize_middle_json,
)


def _source(*, pages: int = 1, assets: int = 0) -> NormalizationSource:
    artifact_id = uuid4()
    return NormalizationSource(
        document_id=uuid5(artifact_id, "1.0:1.0.0"),
        submission_id=uuid4(),
        source_artifact_id=artifact_id,
        source_parser="MinerU",
        source_parser_version="4.0.10",
        source_schema_name="docvortex.middle",
        source_schema_version="2.0",
        source_sha256=hashlib.sha256(b"synthetic original").hexdigest(),
        source_middle_json_sha256=hashlib.sha256(b"synthetic middle").hexdigest(),
        source_page_count=pages,
        source_asset_count=assets,
        source_tier="basic",
    )


def _middle(pages: list[dict[str, object]] | None = None) -> dict[str, object]:
    page_values = pages or [
        {
            "page_idx": 0,
            "blocks": [
                {
                    "type": "text",
                    "index": 0,
                    "bbox": [0.1, 0.1, 0.9, 0.25],
                    "content": "Synthetic lesson text",
                }
            ],
        }
    ]
    return {
        "schema": "docvortex.middle",
        "schema_version": "2.0",
        "metadata": {
            "file_suffix": "pdf",
            "producer": {"name": "mineru", "version": "4.0.10"},
            "document": {},
        },
        "extensions": {"mineru": {"tier": "basic", "parse_mode": "txt"}},
        "pages": page_values,
        "is_full_document": True,
    }


def _manifest(assets: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {"schema": "huipi.parsed-assets", "version": "1", "assets": assets or []}


def _convert(
    middle: dict[str, object],
    *,
    source: NormalizationSource | None = None,
    manifest: dict[str, object] | None = None,
    **limits: int,
):
    return normalize_middle_json(
        middle,
        manifest or _manifest(),
        source or _source(pages=len(middle["pages"])),
        **limits,
    )


def _canonical_bytes(document) -> bytes:
    return json.dumps(
        document.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def test_single_page_text_and_normalized_bbox_are_preserved() -> None:
    document = _convert(_middle())

    page = document.pages[0]
    block = page.blocks[0]
    assert (page.page_index, page.page_number, page.source_page_idx) == (0, 1, 0)
    assert block.content.value == "Synthetic lesson text"
    assert block.normalized_type == "text"
    assert block.source_block_index == 0
    assert block.bbox == (0.1, 0.1, 0.9, 0.25)
    assert document.bbox_coordinate_space == "normalized_page_ratio_0_to_1"


def test_mult_page_order_and_ids_are_deterministic_across_pages() -> None:
    middle = _middle(
        [
            {"page_idx": 0, "blocks": [{"type": "text", "index": 0, "content": "page 1"}]},
            {"page_idx": 1, "blocks": [{"type": "text", "index": 0, "content": "page 2"}]},
        ]
    )
    source = _source(pages=2)

    first = _convert(middle, source=source)
    second = _convert(middle, source=source)

    blocks = [block for page in first.pages for block in page.blocks]
    assert [page.page_number for page in first.pages] == [1, 2]
    assert [block.reading_order for block in blocks] == [0, 1]
    assert [block.content.value for block in blocks] == ["page 1", "page 2"]
    assert blocks[0].block_id != blocks[1].block_id
    assert _canonical_bytes(first) == _canonical_bytes(second)


def test_text_and_inline_formula_are_preserved_as_ordered_typed_nodes() -> None:
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "text",
                        "index": 0,
                        "content": [
                            {"type": "text", "index": 0, "content": "Find "},
                            {"type": "equation_inline", "index": 1, "content": r"x^2+1"},
                            {"type": "text", "index": 2, "content": " = 0."},
                        ],
                    }
                ],
            }
        ]
    )

    block = _convert(middle).pages[0].blocks[0]

    assert [part.normalized_type for part in block.content.children] == [
        "text",
        "formula",
        "text",
    ]
    assert [part.value for part in block.content.children] == ["Find ", r"x^2+1", " = 0."]
    assert [part.source_index for part in block.content.children] == [0, 1, 2]


def test_standalone_formula_content_is_not_rewritten() -> None:
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [{"type": "equation", "index": 0, "content": r"\frac{a}{b}"}],
            }
        ]
    )

    block = _convert(middle).pages[0].blocks[0]

    assert block.normalized_type == "formula"
    assert block.content.value == r"\frac{a}{b}"


def test_table_html_is_preserved_without_flattening() -> None:
    table_html = '<table><tr><td>A</td><td>2</td></tr></table>'
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [{"type": "table", "index": 0, "content": table_html}],
            }
        ]
    )

    block = _convert(middle).pages[0].blocks[0]

    assert block.normalized_type == "table"
    assert block.content.value == table_html


def test_image_asset_uses_stable_logical_id_and_never_serializes_private_key() -> None:
    digest = hashlib.sha256(b"synthetic png").hexdigest()
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "image",
                        "index": 0,
                        "content": [
                            {
                                "type": "image_body",
                                "index": 0,
                                "content": '<img src="figures/diagram.png">',
                            }
                        ],
                    }
                ],
            }
        ]
    )
    manifest = _manifest(
        [
            {
                "path": "figures/diagram.png",
                "object_key": "private/parsed/run/assets/image.png",
                "sha256": digest,
                "size_bytes": 13,
                "content_type": "image/png",
            }
        ]
    )
    source = _source(assets=1)

    document = _convert(middle, source=source, manifest=manifest)
    body = _canonical_bytes(document)
    reference = document.pages[0].blocks[0].asset_refs[0]

    assert reference.kind == "stored"
    assert reference.asset_id == document.assets[0].asset_id
    assert document.assets[0].source_path == "figures/diagram.png"
    assert b"private/parsed/run" not in body


@pytest.mark.parametrize("source_type", ["header", "footer", "doc_title", "page_number"])
def test_layout_types_are_retained_as_layout_blocks(source_type: str) -> None:
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [{"type": source_type, "index": 0, "content": "page furniture"}],
            }
        ]
    )

    block = _convert(middle).pages[0].blocks[0]

    assert block.normalized_type == "layout"
    assert block.source_type == source_type
    assert block.content.value == "page furniture"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda block: block.update(type="brand_new_type"), "invalid_source_structure"),
        (
            lambda block: block.update(
                content=[{"type": "not_a_mineru_span", "content": "x"}]
            ),
            "unsupported_source_block_type",
        ),
        (lambda block: block.update(content={"unexpected": "object"}), "invalid_source_structure"),
    ],
)
def test_unknown_or_malformed_nested_types_fail_closed(mutation, expected_code: str) -> None:
    middle = _middle()
    mutation(middle["pages"][0]["blocks"][0])

    with pytest.raises(CanonicalNormalizationError) as raised:
        _convert(middle)

    assert raised.value.code == expected_code


def test_missing_bbox_stays_null_and_coordinates_are_not_rescaled() -> None:
    middle = _middle()
    middle["pages"][0]["blocks"][0].pop("bbox")

    block = _convert(middle).pages[0].blocks[0]

    assert block.bbox is None


def test_invalid_bbox_is_rejected() -> None:
    middle = _middle()
    middle["pages"][0]["blocks"][0]["bbox"] = [0, 0, 1200, 800]

    with pytest.raises(CanonicalNormalizationError, match="invalid_source_bbox"):
        _convert(middle)


def test_extreme_bbox_integer_is_rejected_without_overflowing() -> None:
    middle = _middle()
    middle["pages"][0]["blocks"][0]["bbox"] = [0, 0, 10**4000, 1]

    with pytest.raises(CanonicalNormalizationError, match="invalid_source_bbox"):
        _convert(middle)


def test_boolean_page_index_is_not_treated_as_zero() -> None:
    middle = _middle()
    middle["pages"][0]["page_idx"] = False

    with pytest.raises(CanonicalNormalizationError, match="invalid_source_structure"):
        _convert(middle)


@pytest.mark.parametrize("version", ["1.0", "2.1", "unknown"])
def test_unknown_middle_schema_version_is_explicitly_rejected(version: str) -> None:
    middle = _middle()
    middle["schema_version"] = version

    with pytest.raises(CanonicalNormalizationError, match="unsupported_source_schema"):
        _convert(middle)


def test_image_path_traversal_and_missing_assets_are_rejected() -> None:
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "image",
                        "index": 0,
                        "image_path": "images/%2e%2e/outside.png",
                        "content": [],
                    }
                ],
            }
        ]
    )

    with pytest.raises(CanonicalNormalizationError, match="invalid_asset_reference"):
        _convert(middle)

    middle["pages"][0]["blocks"][0]["image_path"] = "images/not-in-manifest.png"
    with pytest.raises(CanonicalNormalizationError, match="missing_asset_reference"):
        _convert(middle)


def test_external_image_is_retained_but_never_fetched() -> None:
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "image",
                        "index": 0,
                        "image_url": "https://example.invalid/image.png",
                        "content": [],
                    }
                ],
            }
        ]
    )

    reference = _convert(middle).pages[0].blocks[0].asset_refs[0]

    assert reference.kind == "external"
    assert reference.uri == "https://example.invalid/image.png"


def test_inline_data_reference_is_redacted_not_embedded() -> None:
    middle = _middle(
        [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "image",
                        "index": 0,
                        "image_url": "data:image/png;base64,QUJDREVGRw==",
                        "content": [],
                    }
                ],
            }
        ]
    )

    document = _convert(middle)

    reference = document.pages[0].blocks[0].asset_refs[0]
    assert reference.kind == "inline_redacted"
    assert b"QUJDREVGRw==" not in _canonical_bytes(document)


def test_nested_depth_and_node_count_are_bounded() -> None:
    nested: dict[str, object] = {"type": "text", "index": 0, "content": "deep"}
    for index in reversed(range(4)):
        nested = {
            "type": "text",
            "index": index,
            "content": [nested],
        }
    middle = _middle([{"page_idx": 0, "blocks": [nested]}])

    with pytest.raises(CanonicalNormalizationError, match="normalization_limit_exceeded"):
        _convert(middle, max_nesting_depth=2)
    with pytest.raises(CanonicalNormalizationError, match="normalization_limit_exceeded"):
        _convert(middle, max_nodes=3)


def test_duplicate_json_keys_are_rejected() -> None:
    raw = b'{"schema":"docvortex.middle","schema":"another","schema_version":"2.0"}'

    with pytest.raises(CanonicalNormalizationError, match="invalid_source_json"):
        normalize_middle_json(raw, _manifest(), _source())


def test_private_storage_fields_and_inline_binary_are_redacted_from_source_fields() -> None:
    middle = _middle()
    middle["pages"][0]["blocks"][0]["object_key"] = "secret-private-object-key"
    middle["pages"][0]["blocks"][0]["image_base64"] = "AAECAwQ="

    body = _canonical_bytes(_convert(middle))

    assert b"secret-private-object-key" not in body
    assert b"AAECAwQ=" not in body
