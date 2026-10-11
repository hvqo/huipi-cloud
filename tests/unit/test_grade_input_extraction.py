"""Focused tests for Canonical-to-GradeInput source extraction."""

from uuid import UUID, uuid4

import pytest

from huipi_cloud.modules.answer_review.protocol import (
    ReviewAssetReference,
    ReviewEvidenceRegion,
)
from huipi_cloud.modules.canonical_documents.protocol import (
    CanonicalAsset,
    CanonicalAssetReference,
    CanonicalBlock,
    CanonicalContentNode,
    CanonicalDocument,
    CanonicalPage,
)
from huipi_cloud.modules.grading.service import _extract_confirmed_regions, _InvalidReviewSource


def _document(nodes_by_page: list[list[CanonicalContentNode]]) -> CanonicalDocument:
    document_id = uuid4()
    pages = []
    assets: dict[UUID, CanonicalAsset] = {}
    reading_order = 0
    for page_index, nodes in enumerate(nodes_by_page):
        blocks = []
        for block_index, node in enumerate(nodes):
            for reference in _walk_assets(node):
                if reference.kind == "stored" and reference.asset_id is not None:
                    assets[reference.asset_id] = CanonicalAsset(
                        asset_id=reference.asset_id,
                        source_path=f"images/{reference.asset_id}.png",
                        sha256=reference.sha256 or "f" * 64,
                        size_bytes=reference.size_bytes or 0,
                        content_type=reference.content_type or "image/png",
                    )
            blocks.append(
                CanonicalBlock(
                    block_id=uuid4(),
                    normalized_type=node.normalized_type,
                    reading_order=reading_order,
                    page_index=page_index,
                    page_number=page_index + 1,
                    source_page_idx=page_index,
                    source_block_index=block_index,
                    source_type=node.source_type,
                    content=node,
                )
            )
            reading_order += 1
        pages.append(
            CanonicalPage(
                page_index=page_index,
                page_number=page_index + 1,
                source_page_idx=page_index,
                blocks=blocks,
            )
        )
    return CanonicalDocument(
        document_id=document_id,
        submission_id=uuid4(),
        source_artifact_id=uuid4(),
        source_parser="MinerU",
        source_parser_version="4.0.10",
        source_schema_name="docvortex.middle",
        source_schema_version="2.0",
        source_sha256="a" * 64,
        source_middle_json_sha256="b" * 64,
        page_count=len(pages),
        pages=pages,
        assets=list(assets.values()),
        source_metadata={},
    )


def _walk_assets(node: CanonicalContentNode):
    yield from node.asset_refs
    for child in node.children:
        yield from _walk_assets(child)


def _scalar(text: str, *, source_type: str = "text", normalized_type: str = "text"):
    return CanonicalContentNode(
        normalized_type=normalized_type,
        source_type=source_type,
        content_kind="scalar",
        value=text,
    )


def _region(
    document: CanonicalDocument,
    page_index: int,
    block_index: int,
    pointer: str,
    start,
    end,
):
    page = document.pages[page_index]
    block = page.blocks[block_index]
    node = block.content
    if "/children/" in pointer:
        node = node.children[int(pointer.rsplit("/", 1)[1])]
    references = list(node.asset_refs)
    if node.normalized_type == "image" and not references:
        references = list(block.asset_refs)
    return ReviewEvidenceRegion(
        content_pointer=pointer,
        page_index=page_index,
        page_number=page_index + 1,
        source_block_id=block.block_id,
        reading_order=block.reading_order,
        bbox=node.bbox or block.bbox,
        source_type=node.source_type,
        normalized_type=node.normalized_type,
        text_start=start,
        text_end=end,
        asset_refs=[
            ReviewAssetReference.model_validate(
                item.model_dump(mode="json", exclude={"uri"})
            )
            for item in references
        ],
    )


def test_nested_nodes_with_same_local_offsets_remain_distinct_and_keep_duplicate_text():
    parent = CanonicalContentNode(
        normalized_type="text",
        source_type="paragraph_title",
        content_kind="children",
        children=[
            _scalar("甲乙丙学生答案"),
            _scalar("丁戊己学生答案"),
        ],
    )
    document = _document([[parent]])
    first_pointer = "/pages/0/blocks/0/content/children/0"
    second_pointer = "/pages/0/blocks/0/content/children/1"
    regions = [
        _region(document, 0, 0, first_pointer, 3, 7),
        _region(document, 0, 0, second_pointer, 3, 7),
    ]

    segments, manifest = _extract_confirmed_regions(document, regions)

    assert [item.content_pointer for item in segments] == [first_pointer, second_pointer]
    assert [item.text for item in segments] == ["学生答案", "学生答案"]
    assert sum(len(item.text) for item in segments) == 8
    assert [item.text_start for item in segments] == [3, 3]
    assert [item.representation for item in manifest] == ["text", "text"]


def test_same_node_adjacent_ranges_are_preserved_and_overlap_is_rejected():
    document = _document([[_scalar("甲乙丙丁戊")]])
    pointer = "/pages/0/blocks/0/content"
    adjacent = [
        _region(document, 0, 0, pointer, 0, 2),
        _region(document, 0, 0, pointer, 2, 4),
    ]
    segments, _ = _extract_confirmed_regions(document, adjacent)
    assert [item.text for item in segments] == ["甲乙", "丙丁"]

    overlapping = [
        _region(document, 0, 0, pointer, 0, 3),
        _region(document, 0, 0, pointer, 2, 4),
    ]
    with pytest.raises(_InvalidReviewSource, match="review_region_ranges_overlap"):
        _extract_confirmed_regions(document, overlapping)


def test_text_offsets_use_unicode_codepoints_and_regions_keep_source_order_across_pages():
    document = _document([[_scalar("😀学生答案")], [_scalar("跨页续答")]])
    first_pointer = "/pages/0/blocks/0/content"
    second_pointer = "/pages/1/blocks/0/content"
    regions = [
        _region(document, 1, 0, second_pointer, 0, 3),
        _region(document, 0, 0, first_pointer, 1, 4),
    ]

    segments, _ = _extract_confirmed_regions(document, regions)

    assert [(item.page_index, item.text) for item in segments] == [(0, "学生答"), (1, "跨页续")]


def test_text_range_past_canonical_node_boundary_is_rejected():
    document = _document([[_scalar("短文")]])
    region = _region(document, 0, 0, "/pages/0/blocks/0/content", 0, 3)
    with pytest.raises(_InvalidReviewSource, match="review_region_range_invalid"):
        _extract_confirmed_regions(document, [region])


def test_plain_formula_text_is_typed_but_image_formula_and_table_are_unsupported():
    inline_image = CanonicalAssetReference(
        kind="inline_redacted",
        sha256="c" * 64,
        size_bytes=4,
        content_type="image/png",
        source_pointer="/pages/0/blocks/1/content",
        source_encoding="data_uri",
    )
    document = _document(
        [[
            _scalar("x^2+1", source_type="equation", normalized_type="formula"),
            CanonicalContentNode(
                normalized_type="formula",
                source_type="equation",
                content_kind="scalar",
                value="canonical-inline-image:image/png:dddd:4",
                asset_refs=[inline_image],
            ),
            _scalar("表格行内容", source_type="table", normalized_type="table"),
        ]]
    )
    regions = [
        _region(document, 0, 0, "/pages/0/blocks/0/content", 0, 5),
        _region(document, 0, 1, "/pages/0/blocks/1/content", 0, 35),
        _region(document, 0, 2, "/pages/0/blocks/2/content", 0, 5),
    ]

    segments, manifest = _extract_confirmed_regions(document, regions)

    assert len(segments) == 1
    assert segments[0].kind == "formula_text"
    assert segments[0].text == "x^2+1"
    assert [item.representation for item in manifest] == [
        "formula_text",
        "unsupported",
        "unsupported",
    ]
    assert manifest[1].asset_refs[0].sha256 == "c" * 64
    assert manifest[1].reason_code == "formula_is_not_textual"
    assert manifest[2].reason_code == "table_cells_not_normalized"


def test_pure_image_selection_is_audited_but_never_becomes_text():
    image_asset = CanonicalAssetReference(
        kind="stored",
        asset_id=uuid4(),
        sha256="e" * 64,
        size_bytes=30,
        content_type="image/png",
    )
    image = CanonicalContentNode(
        normalized_type="image",
        source_type="image",
        content_kind="scalar",
        value="",
        asset_refs=[image_asset],
    )
    document = _document([[image]])
    region = _region(document, 0, 0, "/pages/0/blocks/0/content", None, None)

    segments, manifest = _extract_confirmed_regions(document, [region])

    assert segments == []
    assert manifest[0].representation == "unsupported"
    assert manifest[0].reason_code == "image_only_response"
    assert manifest[0].asset_refs[0].asset_id == image_asset.asset_id
