"""Synthetic, manually inspected tests for answer detection and source-safe slicing."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from huipi_cloud.modules.answer_alignment.detector import detect_question_candidates
from huipi_cloud.modules.answer_alignment.errors import AnswerAlignmentArtifactCorruptError
from huipi_cloud.modules.answer_alignment.protocol import CanonicalAssetReference
from huipi_cloud.modules.answer_alignment.repository import AlignmentContext
from huipi_cloud.modules.answer_alignment.segmenter import align_canonical_document
from huipi_cloud.modules.answer_alignment.service import _decode_canonical_document
from huipi_cloud.modules.assignments.models import Question
from huipi_cloud.modules.canonical_documents.protocol import (
    CanonicalAsset,
    CanonicalBlock,
    CanonicalContentNode,
    CanonicalDocument,
    CanonicalPage,
)


def make_document(block_specs: list[list[dict]]) -> CanonicalDocument:
    submission_id = uuid4()
    artifact_id = uuid4()
    document_id = uuid4()
    pages: list[CanonicalPage] = []
    reading_order = 0
    assets_by_id: dict[UUID, CanonicalAsset] = {}
    for page_index, page_spec in enumerate(block_specs):
        blocks = []
        for source_index, spec in enumerate(page_spec):
            content = _make_node(spec)
            for reference in _walk_references(content):
                if reference.kind == "stored" and reference.asset_id is not None:
                    assets_by_id[reference.asset_id] = CanonicalAsset(
                        asset_id=reference.asset_id,
                        source_path=f"images/{reference.asset_id}.png",
                        sha256="c" * 64,
                        size_bytes=10,
                        content_type="image/png",
                    )
            blocks.append(
                CanonicalBlock(
                    block_id=uuid4(),
                    normalized_type=spec.get("block_type", content.normalized_type),
                    reading_order=reading_order,
                    page_index=page_index,
                    page_number=page_index + 1,
                    source_page_idx=page_index,
                    source_block_index=source_index,
                    source_type=spec.get("block_source_type", content.source_type),
                    bbox=(0.0, 0.0, 1.0, 1.0),
                    content=content,
                    asset_refs=list(_walk_references(content)),
                )
            )
            reading_order += 1
        pages.append(
            CanonicalPage(
                page_index=page_index,
                page_number=page_index + 1,
                source_page_idx=page_index,
                blocks=blocks,
                source_fields={},
            )
        )
    return CanonicalDocument(
        document_id=document_id,
        submission_id=submission_id,
        source_artifact_id=artifact_id,
        source_parser="MinerU",
        source_parser_version="4.0.10",
        source_schema_name="docvortex.middle",
        source_schema_version="2.0",
        source_sha256="a" * 64,
        source_middle_json_sha256="b" * 64,
        page_count=len(pages),
        pages=pages,
        assets=list(assets_by_id.values()),
        source_metadata={},
    )


def make_question(number: int, *, assignment_id: UUID | None = None) -> Question:
    return Question(
        id=uuid4(),
        assignment_id=assignment_id or uuid4(),
        question_number=number,
        question_type="essay",
        stem=f"问题 {number}",
        max_score=Decimal("10.00"),
    )


def align(document: CanonicalDocument, questions: list[Question]):
    return align_canonical_document(
        document,
        questions,
        assignment_id=questions[0].assignment_id if questions else uuid4(),
        canonical_sha256="d" * 64,
        alignment_id=uuid4(),
    )


def _make_node(spec: dict) -> CanonicalContentNode:
    if "children" in spec:
        children = [_make_node(child) for child in spec["children"]]
        return CanonicalContentNode(
            normalized_type=spec.get("normalized_type", "text"),
            source_type=spec.get("source_type", "text"),
            content_kind="children",
            children=children,
            asset_refs=spec.get("asset_refs", []),
            source_fields={},
        )
    source_type = spec.get("source_type", "text")
    normalized_type = spec.get("normalized_type", _normalized_for_source(source_type))
    references = spec.get("asset_refs", [])
    return CanonicalContentNode(
        normalized_type=normalized_type,
        source_type=source_type,
        content_kind="scalar",
        value=spec.get("text", ""),
        asset_refs=references,
        source_fields={},
    )


def _normalized_for_source(source_type: str) -> str:
    return {
        "equation": "formula",
        "equation_inline": "formula",
        "image": "image",
        "table": "table",
        "table_body": "table",
        "chart": "chart",
        "list": "list",
        "page_number": "layout",
        "paragraph_title": "layout",
    }.get(source_type, "text")


def _walk_references(node: CanonicalContentNode):
    yield from node.asset_refs
    for child in node.children:
        yield from _walk_references(child)


def test_single_page_two_explicit_questions_align_to_real_question_ids() -> None:
    assignment_id = uuid4()
    questions = [
        make_question(1, assignment_id=assignment_id),
        make_question(2, assignment_id=assignment_id),
    ]
    document = make_document(
        [[{"text": "第1题 答案是4"}, {"text": "第2题 答案是8"}]]
    )

    result = align(document, questions)

    assert result.status == "complete"
    assert [answer.question_id for answer in result.answers] == [q.id for q in questions]
    assert [answer.text_projection for answer in result.answers] == ["答案是4", "答案是8"]
    assert all(answer.matching_status == "aligned" for answer in result.answers)


@pytest.mark.parametrize(
    ("marker", "marker_kind"),
    [
        ("第1题", "explicit_chinese"),
        ("1.", "arabic_dot"),
        ("1、", "chinese_comma"),
        ("1)", "arabic_right_paren"),
        ("(1)", "wrapped_parentheses"),
        ("（1）", "wrapped_parentheses"),
    ],
)
def test_supported_question_marker_forms_are_retained_as_candidates(
    marker: str,
    marker_kind: str,
) -> None:
    document = make_document([[{"text": f"{marker} 答案"}]])

    _, candidates = detect_question_candidates(document)

    assert len(candidates) == 1
    assert candidates[0].marker_text == marker
    assert candidates[0].marker_kind == marker_kind


def test_cross_page_answer_keeps_page_block_and_reading_order_trace() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    document = make_document(
        [[{"text": "第1题 第一步"}], [{"text": "第二步"}, {"text": "结论"}]]
    )

    result = align(document, [question])
    answer = result.answers[0]

    assert answer.matching_status == "aligned"
    assert answer.text_projection == "第一步\n第二步\n结论"
    assert [region.page_index for region in answer.source_regions] == [0, 1, 1]
    assert [region.reading_order for region in answer.source_regions] == [0, 1, 2]
    assert all(region.source_block_id for region in answer.source_regions)


def test_multiple_questions_and_one_answer_can_span_multiple_pages() -> None:
    assignment_id = uuid4()
    questions = [
        make_question(1, assignment_id=assignment_id),
        make_question(2, assignment_id=assignment_id),
    ]
    document = make_document(
        [[{"text": "第1题 第一页答案"}], [{"text": "答案续写"}, {"text": "第2题 第二题作答"}]]
    )

    result = align(document, questions)

    first, second = result.answers
    assert first.text_projection == "第一页答案\n答案续写"
    assert [region.page_index for region in first.source_regions] == [0, 1]
    assert second.text_projection == "第二题作答"
    assert [region.page_index for region in second.source_regions] == [1]


def test_one_text_block_with_two_question_labels_slices_without_overlap() -> None:
    assignment_id = uuid4()
    questions = [
        make_question(1, assignment_id=assignment_id),
        make_question(2, assignment_id=assignment_id),
    ]
    document = make_document([[{"text": "第1题 甲答案\n第2题 乙答案"}]])

    result = align(document, questions)
    first, second = result.answers

    assert first.text_projection == "甲答案"
    assert second.text_projection == "乙答案"
    first_region = first.source_regions[0]
    second_region = second.source_regions[0]
    assert first_region.source_block_id == second_region.source_block_id
    assert first_region.text_end <= second_region.text_start
    assert first_region.text == " 甲答案\n"
    assert second_region.text == " 乙答案"


def test_numbers_inside_equations_fractions_and_prose_are_not_question_labels() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    document = make_document(
        [[
            {"text": "x = 1\n1/2 = 0.5"},
            {"source_type": "equation_inline", "text": "x_1 = \\frac{1}{2}"},
            {"source_type": "page_number", "text": "1"},
        ]]
    )

    atoms, candidates = detect_question_candidates(document)
    result = align(document, [question])

    assert atoms
    assert candidates == []
    assert result.answers[0].matching_status == "not_observed"
    assert "x_1" in result.unassigned_regions[0].text_projection


def test_parenthesized_number_is_review_required_as_possible_subquestion() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    result = align(make_document([[{"text": "（1）由题意可得答案"}]]), [question])

    assert result.answers[0].matching_status == "review_required"
    assert "parenthesized_marker_may_be_subquestion" in result.answers[0].evidence[0].reason_codes
    assert result.status == "review_required"


def test_duplicate_question_number_candidates_are_kept_for_review() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    document = make_document([[{"text": "第1题 第一次答案"}, {"text": "第1题 第二次答案"}]])

    result = align(document, [question])
    answer = result.answers[0]

    assert answer.matching_status == "review_required"
    assert len(answer.evidence) == 2
    assert len(answer.source_regions) == 2
    assert answer.text_projection == "第一次答案\n第二次答案"
    assert all(
        "duplicate_question_number_candidate" in item.reason_codes for item in answer.evidence
    )


def test_non_continuous_database_question_numbers_can_align_in_order() -> None:
    assignment_id = uuid4()
    questions = [
        make_question(2, assignment_id=assignment_id),
        make_question(5, assignment_id=assignment_id),
    ]
    document = make_document([[{"text": "2、答案二"}, {"text": "5、答案五"}]])

    result = align(document, questions)

    assert [answer.question_number for answer in result.answers] == [2, 5]
    assert all(answer.matching_status == "review_required" for answer in result.answers)
    assert all(
        "numbered_marker_may_be_list_item" in answer.evidence[0].reason_codes
        for answer in result.answers
    )


def test_unknown_number_is_unmatched_and_does_not_fill_a_database_question() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    result = align(make_document([[{"text": "第99题 未知答案"}]]), [question])

    assert result.answers[0].matching_status == "not_observed"
    assert result.unassigned_regions[0].reason_code == "question_number_not_in_assignment"
    assert result.unassigned_regions[0].evidence.question_number == 99
    assert "未知答案" in result.unassigned_regions[0].text_projection
    assert result.candidates[0].matching_status == "unmatched"


def test_unknown_number_after_valid_answer_makes_the_previous_boundary_uncertain() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    result = align(
        make_document(
            [[{"text": "第1题 先列等式\n第99题 继续推导\n最后得到 x=2"}]]
        ),
        [question],
    )

    answer = result.answers[0]
    assert answer.matching_status == "review_required"
    assert "answer_end_boundary_is_untrusted" in answer.evidence[0].reason_codes
    assert "继续推导" in result.unassigned_regions[0].text_projection
    assert "最后得到 x=2" in result.unassigned_regions[0].text_projection
    assert result.status == "review_required"


def test_unknown_candidate_between_questions_does_not_make_truncated_answer_aligned() -> None:
    assignment_id = uuid4()
    questions = [
        make_question(1, assignment_id=assignment_id),
        make_question(2, assignment_id=assignment_id),
    ]
    result = align(
        make_document(
            [[{"text": "第1题 第一步\n第99题 可能是续写\n第2题 独立答案"}]]
        ),
        questions,
    )

    first, second = result.answers
    assert first.matching_status == "review_required"
    assert "answer_end_boundary_is_untrusted" in first.evidence[0].reason_codes
    assert second.matching_status == "aligned"
    assert "可能是续写" in result.unassigned_regions[0].text_projection
    assert first.source_regions[0].text_end <= second.source_regions[0].text_start


def test_one_atom_shared_image_keeps_both_questions_in_review() -> None:
    assignment_id = uuid4()
    questions = [
        make_question(1, assignment_id=assignment_id),
        make_question(2, assignment_id=assignment_id),
    ]
    asset_id = uuid4()
    image_reference = CanonicalAssetReference(kind="stored", asset_id=asset_id)
    document = make_document(
        [[
            {
                "text": "第1题 第一段\n第2题 第二段",
                "asset_refs": [image_reference],
            }
        ]]
    )

    result = align(document, questions)
    first, second = result.answers

    assert first.matching_status == second.matching_status == "review_required"
    assert first.asset_refs[0].asset_id == second.asset_refs[0].asset_id == asset_id
    assert "asset_attribution_requires_review" in first.evidence[0].reason_codes
    assert "asset_attribution_requires_review" in second.evidence[0].reason_codes
    assert first.source_regions[0].text_end <= second.source_regions[0].text_start
    assert result.unassigned_regions == []


def test_nested_parent_image_is_preserved_as_unassigned_and_questions_require_review() -> None:
    assignment_id = uuid4()
    questions = [
        make_question(1, assignment_id=assignment_id),
        make_question(2, assignment_id=assignment_id),
    ]
    asset_id = uuid4()
    image_reference = CanonicalAssetReference(kind="stored", asset_id=asset_id)
    document = make_document(
        [[
            {
                "children": [
                    {"text": "第1题 第一段"},
                    {"text": "第2题 第二段"},
                ],
                "asset_refs": [image_reference],
            }
        ]]
    )

    result = align(document, questions)

    assert all(answer.matching_status == "review_required" for answer in result.answers)
    assert all(
        "asset_attribution_requires_review" in answer.evidence[0].reason_codes
        for answer in result.answers
    )
    assert any(
        region.asset_refs and region.asset_refs[0].asset_id == asset_id
        for unassigned in result.unassigned_regions
        for region in unassigned.source_regions
    )


def test_generic_numbered_list_inside_answer_cannot_truncate_a_trusted_answer() -> None:
    assignment_id = uuid4()
    questions = [make_question(1, assignment_id=assignment_id)]
    result = align(
        make_document([[{"text": "第1题 解题过程\n1. 第一步\n结论"}]]),
        questions,
    )

    answer = result.answers[0]
    assert answer.matching_status == "review_required"
    assert any(
        "numbered_marker_may_be_list_item" in evidence.reason_codes
        for evidence in answer.evidence
    )
    assert any(
        "answer_end_boundary_is_untrusted" in evidence.reason_codes
        for evidence in answer.evidence
    )
    assert "结论" in answer.text_projection
    assert result.status == "review_required"


def test_leading_unassigned_text_and_trailing_unknown_candidate_are_preserved() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    result = align(
        make_document(
            [[{"text": "页首说明\n第1题 学生答案\n第88题 不确定尾段"}]]
        ),
        [question],
    )

    assert result.answers[0].matching_status == "review_required"
    all_regions = [
        *result.answers[0].source_regions,
        *(region for item in result.unassigned_regions for region in item.source_regions),
    ]
    all_text = "\n".join(region.text or "" for region in all_regions)
    assert "页首说明" in all_text
    assert "不确定尾段" in all_text
    assert result.unassigned_regions[0].reason_code == "content_before_first_question_candidate"


def test_image_only_answer_region_retains_logical_asset_reference() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    asset_id = uuid4()
    image_reference = CanonicalAssetReference(kind="stored", asset_id=asset_id)
    document = make_document(
        [[{"text": "第1题"}, {"source_type": "image", "text": "", "asset_refs": [image_reference]}]]
    )

    answer = align(document, [question]).answers[0]

    assert answer.matching_status == "aligned"
    assert answer.text_projection == ""
    assert answer.asset_refs[0].asset_id == asset_id
    assert answer.source_regions[-1].normalized_type == "image"
    assert answer.source_regions[-1].asset_refs[0].asset_id == asset_id


def test_formula_and_table_content_remain_traceable_in_answer_regions() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    document = make_document(
        [[
            {"text": "第1题 解答："},
            {"source_type": "equation_inline", "text": "x^2 + 1"},
            {"source_type": "table_body", "text": "<table><tr><td>2</td></tr></table>"},
        ]]
    )

    answer = align(document, [question]).answers[0]

    assert "x^2 + 1" in answer.text_projection
    assert "<table>" in answer.text_projection
    assert {region.normalized_type for region in answer.source_regions} >= {"formula", "table"}
    assert all(
        region.content_pointer.startswith("/pages/0/blocks/") for region in answer.source_regions
    )


def test_canonical_source_checksum_mismatch_is_rejected_before_alignment() -> None:
    canonical = make_document([[{"text": "第1题 学生答案"}]])
    payload = canonical.model_dump_json().encode("utf-8")
    canonical_index = SimpleNamespace(
        id=canonical.document_id,
        parsed_artifact_id=canonical.source_artifact_id,
        schema_version=canonical.schema_version,
        source_sha256=canonical.source_sha256,
        page_count=canonical.page_count,
        block_count=sum(len(page.blocks) for page in canonical.pages),
        canonical_sha256="e" * 64,
    )
    context = AlignmentContext(
        submission_id=canonical.submission_id,
        assignment_id=uuid4(),
        parsing_status="succeeded",
        canonical_artifact=canonical_index,
        questions=[],
    )

    with pytest.raises(AnswerAlignmentArtifactCorruptError):
        _decode_canonical_document(payload, context)


def test_complex_same_block_split_is_preserved_but_requires_review() -> None:
    assignment_id = uuid4()
    questions = [
        make_question(1, assignment_id=assignment_id),
        make_question(2, assignment_id=assignment_id),
    ]
    document = make_document(
        [[
            {
                "normalized_type": "text",
                "source_type": "text",
                "children": [
                    {"text": "第1题 答案一"},
                    {"source_type": "equation", "text": "x=1"},
                    {"text": "第2题 答案二"},
                ],
            }
        ]]
    )

    result = align(document, questions)

    assert all(answer.matching_status == "review_required" for answer in result.answers)
    assert "complex_block_split_requires_review" in result.candidates[0].reason_codes
    assert (
        result.answers[0].source_regions[0].source_block_id
        == result.answers[1].source_regions[0].source_block_id
    )
    assert (
        result.answers[0].source_regions[0].content_pointer
        != result.answers[1].source_regions[-1].content_pointer
    )
    assert result.answers[0].source_regions[0].text == " 答案一"
    assert result.answers[1].source_regions[-1].text == " 答案二"


def test_question_order_conflict_marks_candidates_for_review() -> None:
    assignment_id = uuid4()
    questions = [
        make_question(1, assignment_id=assignment_id),
        make_question(2, assignment_id=assignment_id),
    ]
    result = align(make_document([[{"text": "第2题 后写"}, {"text": "第1题 先写"}]]), questions)

    assert all(answer.matching_status == "review_required" for answer in result.answers)
    assert all("question_sequence_out_of_order" in c.reason_codes for c in result.candidates)


def test_list_items_are_not_treated_as_top_level_question_labels() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    document = make_document(
        [[{"source_type": "list", "normalized_type": "list", "text": "1. 列表项"}]]
    )

    atoms, candidates = detect_question_candidates(document)
    result = align(document, [question])

    assert atoms
    assert candidates == []
    assert result.answers[0].matching_status == "not_observed"
    assert result.unassigned_regions[0].text_projection == "1. 列表项"


def test_short_generic_label_requires_more_sequence_evidence() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    result = align(make_document([[{"text": "1. 结果"}]]), [question])

    assert result.answers[0].matching_status == "review_required"
    assert "insufficient_sequence_evidence" in result.answers[0].evidence[0].reason_codes


def test_result_has_real_question_ids_and_never_includes_answer_key_data() -> None:
    assignment_id = uuid4()
    question = make_question(1, assignment_id=assignment_id)
    result = align(make_document([[{"text": "第1题 学生内容"}]]), [question])
    serialized = result.model_dump_json()

    assert result.answers[0].question_id == question.id
    assert "answer_content" not in serialized
    assert "标准答案" not in serialized
