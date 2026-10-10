"""Keep a review's selected source separate from other Questions' regions."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from huipi_cloud.modules.answer_alignment.protocol import AlignedAnswer, SourceRegion
from huipi_cloud.modules.answer_review import service as answer_review_service
from huipi_cloud.modules.answer_review.errors import AnswerReviewRegionInvalidError
from huipi_cloud.modules.answer_review.protocol import (
    AnswerReviewDecisionCreate,
    ReviewRegionSelection,
)
from huipi_cloud.modules.answer_review.service import (
    _index_canonical_nodes,
    _materialize_regions,
    _validate_question_ownership,
)
from huipi_cloud.modules.canonical_documents.protocol import (
    CanonicalAsset,
    CanonicalAssetReference,
    CanonicalBlock,
    CanonicalContentNode,
    CanonicalDocument,
    CanonicalPage,
)


def _payload(decision: str, pointer: str, *, role: str, reason: str):
    return AnswerReviewDecisionCreate(
        decision=decision,
        response_regions=[{"content_pointer": pointer, "text_start": 0, "text_end": 2}]
        if role == "response"
        else [],
        uncertain_regions=[{"content_pointer": pointer, "text_start": 0, "text_end": 2}]
        if role == "uncertain"
        else [],
        reason_codes=[reason],
        reviewer_ref="local-reviewer",
    )


def _canonical_parent_with_two_questions() -> tuple[
    CanonicalDocument,
    str,
    str,
    str,
    str,
    str,
    str,
    UUID,
    CanonicalAssetReference,
]:
    asset_id = uuid4()
    asset_ref = CanonicalAssetReference(kind="stored", asset_id=asset_id)
    block_id = uuid4()
    parent_pointer = "/pages/0/blocks/0/content"
    first_pointer = f"{parent_pointer}/children/0"
    second_pointer = f"{parent_pointer}/children/1"
    empty_pointer = f"{parent_pointer}/children/2"
    image_pointer = f"{parent_pointer}/children/3"
    duplicate_image_pointer = f"{parent_pointer}/children/4"
    root = CanonicalContentNode(
        normalized_type="layout",
        source_type="composite",
        content_kind="children",
        children=[
            CanonicalContentNode(
                normalized_type="text",
                source_type="span",
                content_kind="scalar",
                value="Q1 answer",
            ),
            CanonicalContentNode(
                normalized_type="text",
                source_type="span",
                content_kind="scalar",
                value="Q2 answer",
            ),
            CanonicalContentNode(
                normalized_type="text",
                source_type="span",
                content_kind="scalar",
                value="",
            ),
            CanonicalContentNode(
                normalized_type="image",
                source_type="image",
                content_kind="scalar",
                value="",
                asset_refs=[asset_ref],
            ),
            CanonicalContentNode(
                normalized_type="image",
                source_type="image",
                content_kind="scalar",
                value="",
                asset_refs=[asset_ref],
            ),
        ],
    )
    block = CanonicalBlock(
        block_id=block_id,
        normalized_type="layout",
        reading_order=0,
        page_index=0,
        page_number=1,
        source_page_idx=0,
        source_block_index=0,
        source_type="composite",
        content=root,
        asset_refs=[asset_ref],
    )
    document = CanonicalDocument(
        document_id=uuid4(),
        submission_id=uuid4(),
        source_artifact_id=uuid4(),
        source_parser="mineru",
        source_parser_version="4.0.10",
        source_schema_name="mineru.middle_json",
        source_schema_version="4.0.10",
        source_sha256="a" * 64,
        source_middle_json_sha256="b" * 64,
        page_count=1,
        pages=[
            CanonicalPage(
                page_index=0,
                page_number=1,
                source_page_idx=0,
                blocks=[block],
            )
        ],
        assets=[
            CanonicalAsset(
                asset_id=asset_id,
                source_path="images/shared.png",
                sha256="c" * 64,
                size_bytes=12,
                content_type="image/png",
            )
        ],
        source_metadata={},
    )
    return (
        document,
        parent_pointer,
        first_pointer,
        second_pointer,
        empty_pointer,
        image_pointer,
        duplicate_image_pointer,
        block_id,
        asset_ref,
    )


def _source(pointer: str, start: int, end: int, block_id: UUID) -> SourceRegion:
    return SourceRegion(
        page_index=0,
        source_block_id=block_id,
        reading_order=0,
        source_type="span",
        normalized_type="text",
        content_pointer=pointer,
        text_start=start,
        text_end=end,
    )


def _answer(question_id: UUID, source: SourceRegion) -> AlignedAnswer:
    return AlignedAnswer(
        question_id=question_id,
        question_number=1,
        question_type="short_answer",
        matching_status="aligned",
        source_regions=[source],
    )


def _independent_image_on_second_page(document: CanonicalDocument):
    asset_id = uuid4()
    asset_ref = CanonicalAssetReference(kind="stored", asset_id=asset_id)
    image_node = CanonicalContentNode(
        normalized_type="image",
        source_type="image",
        content_kind="scalar",
        value="",
        asset_refs=[asset_ref],
    )
    image_block = CanonicalBlock(
        block_id=uuid4(),
        normalized_type="image",
        reading_order=1,
        page_index=1,
        page_number=2,
        source_page_idx=1,
        source_block_index=0,
        source_type="image",
        content=image_node,
    )
    second_page = CanonicalPage(
        page_index=1,
        page_number=2,
        source_page_idx=1,
        blocks=[image_block],
    )
    asset = CanonicalAsset(
        asset_id=asset_id,
        source_path="images/independent.png",
        sha256="d" * 64,
        size_bytes=8,
        content_type="image/png",
    )
    two_page_document = CanonicalDocument.model_validate(
        {
            **document.model_dump(mode="python"),
            "page_count": 2,
            "pages": [document.pages[0], second_page],
            "assets": [*document.assets, asset],
        }
    )
    return two_page_document, "/pages/1/blocks/0/content", image_block.block_id


def test_question_owned_source_cannot_be_confirmed_under_another_question() -> None:
    question_one = uuid4()
    question_two = uuid4()
    pointer = "/pages/0/blocks/4/content"
    answers = [
        SimpleNamespace(question_id=question_one, source_regions=[]),
        SimpleNamespace(
            question_id=question_two,
            source_regions=[SimpleNamespace(content_pointer=pointer, text_start=0, text_end=2)],
        ),
    ]
    payload = _payload(
        "response_present",
        pointer,
        role="response",
        reason="student_work_visible",
    )

    with pytest.raises(AnswerReviewRegionInvalidError):
        _validate_question_ownership(payload, question_one, answers, [], {})


def test_other_question_source_can_only_be_recorded_as_explicit_uncertainty() -> None:
    question_one = uuid4()
    question_two = uuid4()
    pointer = "/pages/0/blocks/4/content"
    answers = [
        SimpleNamespace(question_id=question_one, source_regions=[]),
        SimpleNamespace(
            question_id=question_two,
            source_regions=[SimpleNamespace(content_pointer=pointer, text_start=0, text_end=2)],
        ),
    ]
    payload = _payload(
        "uncertain",
        pointer,
        role="uncertain",
        reason="response_not_linked_to_question",
    )

    _validate_question_ownership(payload, question_one, answers, [], {})


def test_unassigned_source_requires_uncertain_response_link_reason() -> None:
    question_id = uuid4()
    pointer = "/pages/0/blocks/4/content"
    answers = [SimpleNamespace(question_id=question_id, source_regions=[])]
    unassigned = [
        SimpleNamespace(
            source_regions=[SimpleNamespace(content_pointer=pointer, text_start=0, text_end=2)]
        )
    ]
    invalid = _payload(
        "uncertain",
        pointer,
        role="uncertain",
        reason="ambiguous_handwriting",
    )

    with pytest.raises(AnswerReviewRegionInvalidError):
        _validate_question_ownership(invalid, question_id, answers, unassigned, {})


def test_shared_node_with_disjoint_question_ranges_keeps_distinct_ownership() -> None:
    question_one = uuid4()
    question_two = uuid4()
    pointer = "/pages/0/blocks/4/content"
    answers = [
        SimpleNamespace(question_id=question_one, source_regions=[]),
        SimpleNamespace(
            question_id=question_two,
            source_regions=[SimpleNamespace(content_pointer=pointer, text_start=5, text_end=8)],
        ),
    ]
    payload = _payload(
        "response_present",
        pointer,
        role="response",
        reason="student_work_visible",
    )

    _validate_question_ownership(payload, question_one, answers, [], {})


def test_parent_container_cannot_claim_a_sibling_questions_child() -> None:
    document, parent_pointer, first_pointer, second_pointer, _, _, _, block_id, _ = (
        _canonical_parent_with_two_questions()
    )
    question_one = uuid4()
    question_two = uuid4()
    answers = [
        _answer(question_one, _source(first_pointer, 0, 2, block_id)),
        _answer(question_two, _source(second_pointer, 0, 2, block_id)),
    ]
    payload = AnswerReviewDecisionCreate(
        decision="response_present",
        response_regions=[{"content_pointer": parent_pointer}],
        reason_codes=["student_work_visible"],
        reviewer_ref="local-reviewer",
    )

    with pytest.raises(AnswerReviewRegionInvalidError):
        _validate_question_ownership(
            payload,
            question_one,
            answers,
            [],
            _index_canonical_nodes(document),
        )


def test_question_two_child_cannot_be_confirmed_as_question_one_response() -> None:
    document, _, first_pointer, second_pointer, _, _, _, block_id, _ = (
        _canonical_parent_with_two_questions()
    )
    question_one = uuid4()
    question_two = uuid4()
    answers = [
        _answer(question_one, _source(first_pointer, 0, 2, block_id)),
        _answer(question_two, _source(second_pointer, 0, 2, block_id)),
    ]
    payload = AnswerReviewDecisionCreate(
        decision="response_present",
        response_regions=[
            {"content_pointer": second_pointer, "text_start": 0, "text_end": 2}
        ],
        reason_codes=["student_work_visible"],
        reviewer_ref="local-reviewer",
    )

    with pytest.raises(AnswerReviewRegionInvalidError):
        _validate_question_ownership(
            payload,
            question_one,
            answers,
            [],
            _index_canonical_nodes(document),
        )


def test_block_asset_does_not_make_non_image_child_selectable_as_whole_node() -> None:
    document, _, _, _, empty_pointer, _, _, _, _ = _canonical_parent_with_two_questions()
    nodes = _index_canonical_nodes(document)

    with pytest.raises(AnswerReviewRegionInvalidError):
        _materialize_regions([ReviewRegionSelection(content_pointer=empty_pointer)], nodes)


def test_sibling_text_ranges_keep_question_ownership_separate() -> None:
    document, _, first_pointer, second_pointer, _, _, _, block_id, _ = (
        _canonical_parent_with_two_questions()
    )
    question_one = uuid4()
    question_two = uuid4()
    answers = [
        _answer(question_one, _source(first_pointer, 0, 2, block_id)),
        _answer(question_two, _source(second_pointer, 0, 2, block_id)),
    ]
    payload = _payload(
        "response_present",
        first_pointer,
        role="response",
        reason="student_work_visible",
    )

    _validate_question_ownership(
        payload,
        question_one,
        answers,
        [],
        _index_canonical_nodes(document),
    )


def test_shared_image_is_uncertain_not_question_specific_response() -> None:
    document, _, first_pointer, second_pointer, _, image_pointer, _, block_id, asset_ref = (
        _canonical_parent_with_two_questions()
    )
    nodes = _index_canonical_nodes(document)
    question_one = uuid4()
    question_two = uuid4()
    answers = [
        _answer(question_one, _source(first_pointer, 0, 2, block_id)),
        _answer(
            question_two,
            _source(second_pointer, 0, 2, block_id).model_copy(update={"asset_refs": [asset_ref]}),
        ),
    ]
    confirmed = AnswerReviewDecisionCreate(
        decision="response_present",
        response_regions=[{"content_pointer": image_pointer}],
        reason_codes=["diagram_marks_visible"],
        reviewer_ref="local-reviewer",
    )
    with pytest.raises(AnswerReviewRegionInvalidError):
        _validate_question_ownership(confirmed, question_one, answers, [], nodes)

    uncertain = AnswerReviewDecisionCreate(
        decision="uncertain",
        uncertain_regions=[{"content_pointer": image_pointer}],
        reason_codes=["shared_figure_attribution_unclear"],
        reviewer_ref="local-reviewer",
    )
    _validate_question_ownership(uncertain, question_one, answers, [], nodes)
    materialized = _materialize_regions(uncertain.uncertain_regions, nodes)
    assert len(materialized[0].asset_refs) == 1
    assert materialized[0].asset_refs[0].asset_id == asset_ref.asset_id


def test_shared_block_image_container_is_citable_only_as_uncertain() -> None:
    document, parent_pointer, first_pointer, second_pointer, _, _, _, block_id, asset_ref = (
        _canonical_parent_with_two_questions()
    )
    nodes = _index_canonical_nodes(document)
    question_one = uuid4()
    question_two = uuid4()
    answers = [
        _answer(question_one, _source(first_pointer, 0, 2, block_id)),
        _answer(question_two, _source(second_pointer, 0, 2, block_id)),
    ]
    confirmed = AnswerReviewDecisionCreate(
        decision="response_present",
        response_regions=[{"content_pointer": parent_pointer}],
        reason_codes=["student_work_visible"],
        reviewer_ref="local-reviewer",
    )
    with pytest.raises(AnswerReviewRegionInvalidError):
        _materialize_regions(confirmed.response_regions, nodes)

    uncertain = AnswerReviewDecisionCreate(
        decision="uncertain",
        uncertain_regions=[{"content_pointer": parent_pointer}],
        reason_codes=["shared_figure_attribution_unclear"],
        reviewer_ref="local-reviewer",
    )
    region = _materialize_regions(
        uncertain.uncertain_regions,
        nodes,
        allow_block_asset_context=True,
    )
    _validate_question_ownership(uncertain, question_one, answers, [], nodes)

    assert region[0].content_pointer == parent_pointer
    assert len(region[0].asset_refs) == 1
    assert region[0].asset_refs[0].asset_id == asset_ref.asset_id


def test_independent_image_and_single_question_text_remain_selectable() -> None:
    document, _, first_pointer, _, _, image_pointer, _, _, _ = (
        _canonical_parent_with_two_questions()
    )
    nodes = _index_canonical_nodes(document)

    image_region = _materialize_regions(
        [ReviewRegionSelection(content_pointer=image_pointer)],
        nodes,
    )
    text_region = _materialize_regions(
        [ReviewRegionSelection(content_pointer=first_pointer, text_start=0, text_end=2)],
        nodes,
    )

    assert image_region[0].normalized_type == "image"
    assert len(image_region[0].asset_refs) == 1
    assert text_region[0].text_start == 0
    assert text_region[0].text_end == 2


def test_independent_image_node_does_not_conflict_with_other_question_text() -> None:
    document, _, first_pointer, second_pointer, _, _, _, block_id, _ = (
        _canonical_parent_with_two_questions()
    )
    document, image_pointer, _ = _independent_image_on_second_page(document)
    question_one = uuid4()
    question_two = uuid4()
    answers = [
        _answer(question_one, _source(first_pointer, 0, 2, block_id)),
        _answer(question_two, _source(second_pointer, 0, 2, block_id)),
    ]
    payload = AnswerReviewDecisionCreate(
        decision="response_present",
        response_regions=[{"content_pointer": image_pointer}],
        reason_codes=["diagram_marks_visible"],
        reviewer_ref="local-reviewer",
    )
    nodes = _index_canonical_nodes(document)

    _materialize_regions(payload.response_regions, nodes)
    _validate_question_ownership(payload, question_one, answers, [], nodes)


def test_parent_child_overlap_between_response_and_prompt_is_rejected() -> None:
    document, parent_pointer, first_pointer, _, _, _, _, _, _ = (
        _canonical_parent_with_two_questions()
    )
    payload = AnswerReviewDecisionCreate(
        decision="response_present",
        response_regions=[{"content_pointer": parent_pointer}],
        excluded_prompt_regions=[
            {"content_pointer": first_pointer, "text_start": 0, "text_end": 2}
        ],
        reason_codes=["student_work_visible"],
        reviewer_ref="local-reviewer",
    )

    with pytest.raises(AnswerReviewRegionInvalidError):
        answer_review_service._validate_selection_conflicts(
            payload,
            _index_canonical_nodes(document),
        )


def test_text_and_distinct_image_evidence_do_not_conflict() -> None:
    document, _, first_pointer, _, _, image_pointer, _, _, _ = (
        _canonical_parent_with_two_questions()
    )
    payload = AnswerReviewDecisionCreate(
        decision="response_present",
        response_regions=[
            {"content_pointer": first_pointer, "text_start": 0, "text_end": 2}
        ],
        excluded_prompt_regions=[{"content_pointer": image_pointer}],
        reason_codes=["student_work_visible"],
        reviewer_ref="local-reviewer",
    )

    answer_review_service._validate_selection_conflicts(
        payload,
        _index_canonical_nodes(document),
    )


def test_same_image_selected_from_block_and_child_is_deduplicated() -> None:
    document, _, _, _, _, image_pointer, _, _, _ = _canonical_parent_with_two_questions()
    nodes = _index_canonical_nodes(document)

    evidence = _materialize_regions(
        [ReviewRegionSelection(content_pointer=image_pointer)],
        nodes,
    )

    assert len(evidence[0].asset_refs) == 1


def test_one_image_asset_cannot_be_split_between_response_and_prompt_roles() -> None:
    document, _, _, _, _, image_pointer, duplicate_pointer, _, _ = (
        _canonical_parent_with_two_questions()
    )
    payload = AnswerReviewDecisionCreate(
        decision="response_present",
        response_regions=[{"content_pointer": image_pointer}],
        excluded_prompt_regions=[{"content_pointer": duplicate_pointer}],
        reason_codes=["diagram_marks_visible"],
        reviewer_ref="local-reviewer",
    )

    with pytest.raises(AnswerReviewRegionInvalidError):
        answer_review_service._validate_selection_conflicts(
            payload,
            _index_canonical_nodes(document),
        )


def test_independent_cross_page_regions_do_not_overlap() -> None:
    document, _, first_pointer, _, _, _, _, _, _ = _canonical_parent_with_two_questions()
    first_block = document.pages[0].blocks[0]
    second_block_id = uuid4()
    second_block = first_block.model_copy(
        update={
            "block_id": second_block_id,
            "reading_order": 1,
            "page_index": 1,
            "page_number": 2,
            "source_page_idx": 1,
            "source_block_index": 0,
        }
    )
    second_page = CanonicalPage(
        page_index=1,
        page_number=2,
        source_page_idx=1,
        blocks=[second_block],
    )
    two_page_document = CanonicalDocument.model_validate(
        {
            **document.model_dump(mode="python"),
            "page_count": 2,
            "pages":[document.pages[0], second_page],
        }
    )
    second_pointer = "/pages/1/blocks/0/content/children/0"
    payload = AnswerReviewDecisionCreate(
        decision="response_present",
        response_regions=[
            {"content_pointer": first_pointer, "text_start": 0, "text_end": 2},
            {"content_pointer": second_pointer, "text_start": 2, "text_end": 5},
        ],
        reason_codes=["cross_page_response", "multiple_response_regions"],
        reviewer_ref="local-reviewer",
    )

    answer_review_service._validate_selection_conflicts(
        payload,
        _index_canonical_nodes(two_page_document),
    )


def test_protocol_rejects_response_and_uncertain_roles_but_allows_adjacent_text() -> None:
    pointer = "/pages/0/blocks/0/content"
    with pytest.raises(ValueError, match="cannot include uncertain"):
        AnswerReviewDecisionCreate(
            decision="response_present",
            response_regions=[{"content_pointer": pointer, "text_start": 0, "text_end": 2}],
            uncertain_regions=[{"content_pointer": pointer, "text_start": 2, "text_end": 4}],
            reason_codes=["student_work_visible"],
            reviewer_ref="local-reviewer",
        )

    payload = AnswerReviewDecisionCreate(
        decision="response_present",
        response_regions=[{"content_pointer": pointer, "text_start": 0, "text_end": 2}],
        excluded_prompt_regions=[
            {"content_pointer": pointer, "text_start": 2, "text_end": 4}
        ],
        reason_codes=["student_work_visible"],
        reviewer_ref="local-reviewer",
    )
    assert len(payload.response_regions) == len(payload.excluded_prompt_regions) == 1

    with pytest.raises(ValueError, match="cannot include uncertain"):
        AnswerReviewDecisionCreate(
            decision="response_present",
            response_regions=[{"content_pointer": pointer}],
            uncertain_regions=[
                {
                    "content_pointer": f"{pointer}/children/0",
                    "text_start": 0,
                    "text_end": 2,
                }
            ],
            reason_codes=["student_work_visible"],
            reviewer_ref="local-reviewer",
        )

    with pytest.raises(ValueError, match="overlap or repeat"):
        AnswerReviewDecisionCreate(
            decision="response_present",
            response_regions=[{"content_pointer": pointer, "text_start": 0, "text_end": 3}],
            excluded_prompt_regions=[
                {"content_pointer": pointer, "text_start": 2, "text_end": 4}
            ],
            reason_codes=["student_work_visible"],
            reviewer_ref="local-reviewer",
        )
