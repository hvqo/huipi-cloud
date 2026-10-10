from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError
from pypdf import PdfWriter

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.visual_evidence.provider import (
    AlignmentRegionSummary,
    OllamaNativeVisionProvider,
    OpenAICompatibleVisionProvider,
    VisionProviderResponse,
)
from huipi_cloud.infrastructure.visual_evidence.render import (
    RenderedPage,
    render_selected_pages,
    transform_bbox_to_source,
)
from huipi_cloud.modules.answer_alignment.protocol import AlignedAnswer, SourceRegion
from huipi_cloud.modules.canonical_documents.protocol import (
    CanonicalAsset,
    CanonicalAssetReference,
    CanonicalBlock,
    CanonicalContentNode,
    CanonicalDocument,
    CanonicalPage,
)
from huipi_cloud.modules.visual_evidence.errors import (
    VisualEvidenceConfigurationError,
    VisualEvidenceInputError,
    VisualEvidenceLimitError,
    VisualEvidenceProtocolError,
    VisualEvidenceProviderError,
)
from huipi_cloud.modules.visual_evidence.protocol import (
    VisualEvidenceModelRegion,
    VisualEvidenceModelReply,
)
from huipi_cloud.modules.visual_evidence.service import _attribute_regions, _boxes_match
from tests.visual_samples import create_visual_samples


def _response(
    content: str,
    *,
    status_code: int = 200,
    usage: dict[str, int] | None = None,
) -> httpx.Response:
    body: dict[str, object] = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(status_code, json=body)


def _good_reply() -> str:
    return json.dumps(
        {
            "outcome": "candidate_response_present",
            "visual_regions": [
                {
                    "page_index": 0,
                    "visual_bbox": [0.12, 0.12, 0.4, 0.42],
                    "evidence_type": "handwritten_text",
                    "reason_codes": ["handwriting_visible"],
                }
            ],
            "reason_codes": ["visual_evidence_candidate_only"],
        }
    )


def _image_page(index: int = 0) -> RenderedPage:
    return RenderedPage(
        page_index=index,
        image_bytes=b"synthetic-rendered-image",
        content_type="image/jpeg",
        source_width=1000,
        source_height=1000,
        source_unit="image_pixels",
        source_rotation_degrees=0,
        rendered_width=1000,
        rendered_height=1000,
        effective_dpi=None,
        exif_orientation=1,
        mapping_eligible=True,
        mapping_reason=None,
        transform=(1, 0, 0, 0, 1, 0),
        sha256="a" * 64,
    )


def _canonical(*, with_shared_image: bool = False) -> tuple[CanonicalDocument, UUIDPair]:
    submission_id = uuid4()
    parsed_id = uuid4()
    doc_id = uuid4()
    block_id = uuid4()
    image_id = uuid4()
    if with_shared_image:
        child_nodes = [
            CanonicalContentNode(
                normalized_type="image",
                source_type="image",
                content_kind="children",
                bbox=(0.1, 0.1, 0.45, 0.45),
                asset_refs=[CanonicalAssetReference(kind="stored", asset_id=image_id)],
            ),
            CanonicalContentNode(
                normalized_type="image",
                source_type="image",
                content_kind="children",
                bbox=(0.1, 0.1, 0.45, 0.45),
                asset_refs=[CanonicalAssetReference(kind="stored", asset_id=image_id)],
            ),
        ]
        assets = [
            CanonicalAsset(
                asset_id=image_id,
                source_path="figures/shared.png",
                sha256="b" * 64,
                size_bytes=4,
                content_type="image/png",
            )
        ]
    else:
        child_nodes = [
            CanonicalContentNode(
                normalized_type="text",
                source_type="span",
                content_kind="scalar",
                value="Question one response",
                bbox=(0.1, 0.1, 0.45, 0.45),
                source_index=0,
            ),
            CanonicalContentNode(
                normalized_type="text",
                source_type="span",
                content_kind="scalar",
                value="Question two response",
                bbox=(0.55, 0.55, 0.9, 0.9),
                source_index=1,
            ),
        ]
        assets = []
    root = CanonicalContentNode(
        normalized_type="text" if not with_shared_image else "image",
        source_type="paragraph" if not with_shared_image else "image_group",
        content_kind="children",
        bbox=(0.0, 0.0, 1.0, 1.0),
        children=child_nodes,
    )
    block = CanonicalBlock(
        block_id=block_id,
        normalized_type="text" if not with_shared_image else "image",
        reading_order=0,
        page_index=0,
        page_number=1,
        source_page_idx=0,
        source_block_index=0,
        source_type=root.source_type,
        bbox=(0.0, 0.0, 1.0, 1.0),
        content=root,
    )
    document = CanonicalDocument(
        document_id=doc_id,
        submission_id=submission_id,
        source_artifact_id=parsed_id,
        source_parser="MinerU",
        source_parser_version="4.0.10",
        source_schema_name="docvortex.middle",
        source_schema_version="2.0",
        source_sha256="c" * 64,
        source_middle_json_sha256="d" * 64,
        page_count=1,
        pages=[CanonicalPage(page_index=0, page_number=1, source_page_idx=0, blocks=[block])],
        assets=assets,
        source_metadata={},
    )
    return document, UUIDPair(submission_id, doc_id, block_id, image_id)


class UUIDPair:
    def __init__(self, submission_id, doc_id, block_id, image_id) -> None:
        self.submission_id = submission_id
        self.doc_id = doc_id
        self.block_id = block_id
        self.image_id = image_id


def _source_region(pointer: str, block_id, bbox, *, asset_refs=None) -> SourceRegion:
    return SourceRegion(
        page_index=0,
        source_block_id=block_id,
        reading_order=0,
        bbox=bbox,
        source_type="paragraph",
        normalized_type="image" if asset_refs else "text",
        content_pointer=pointer,
        text_start=0 if not asset_refs else None,
        text_end=1 if not asset_refs else None,
        text="source text" if not asset_refs else None,
        asset_refs=asset_refs or [],
    )


def test_pdf_renders_only_selected_page_and_records_rotation(tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    rotated = writer.add_blank_page(width=612, height=792)
    rotated.rotate(90)
    source = tmp_path / "two-pages.pdf"
    with source.open("wb") as stream:
        writer.write(stream)

    pages = render_selected_pages(
        source,
        content_type="application/pdf",
        selected_page_indexes=[1],
        config=Settings(),
    )
    assert len(pages) == 1
    assert pages[0].page_index == 1
    assert pages[0].source_width == 612
    assert pages[0].source_height == 792
    assert pages[0].source_rotation_degrees == 90
    assert (pages[0].rendered_width, pages[0].rendered_height) == (1320, 1020)
    assert not pages[0].mapping_eligible
    assert pages[0].mapping_reason == "rotated_pdf_page"


def test_pdf_page_selection_rejects_page_outside_document(tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    source = tmp_path / "one.pdf"
    with source.open("wb") as stream:
        writer.write(stream)
    with pytest.raises(VisualEvidenceInputError, match="selected_pdf_page_not_found"):
        render_selected_pages(
            source, content_type="application/pdf", selected_page_indexes=[1]
        )


def test_png_and_jpeg_samples_render_as_bounded_rgb_images(tmp_path: Path) -> None:
    samples = create_visual_samples(tmp_path / "samples")
    assert set(samples) == {
        "print_only",
        "mixed_handwriting",
        "formula",
        "geometry",
        "blank_region",
    }
    for path in samples.values():
        page = render_selected_pages(
            path, content_type="image/png", selected_page_indexes=[0]
        )[0]
        assert page.page_index == 0
        assert page.content_type == "image/jpeg"
        assert page.rendered_width <= 2048 and page.rendered_height <= 2048
        assert len(page.image_bytes) <= 3 * 1024 * 1024
        assert len(page.sha256) == 64


def test_exif_orientation_is_applied_and_transform_is_recorded(tmp_path: Path) -> None:
    raw = Image.new("RGB", (300, 200), "white")
    exif = raw.getexif()
    exif[274] = 6
    path = tmp_path / "oriented.jpg"
    raw.save(path, format="JPEG", exif=exif)

    page = render_selected_pages(
        path, content_type="image/jpeg", selected_page_indexes=[0]
    )[0]
    assert (page.source_width, page.source_height) == (300, 200)
    assert (page.rendered_width, page.rendered_height) == (200, 300)
    assert page.exif_orientation == 6
    assert not page.mapping_eligible
    assert page.transform == (0, 1, 0, -1, 0, 1)


def test_affine_transform_restores_crop_and_rejects_outside_bbox() -> None:
    restored = transform_bbox_to_source(
        (0.1, 0.1, 0.9, 0.9),
        (0.6, 0, 0.2, 0, 0.4, 0.3),
    )
    assert restored == pytest.approx((0.26, 0.34, 0.74, 0.66))
    with pytest.raises(VisualEvidenceInputError, match="bbox_transform_out_of_bounds"):
        transform_bbox_to_source((0.5, 0.5, 1.0, 1.0), (1, 0, 0.5, 0, 1, 0))


def test_pixel_limit_rejects_large_raster_before_decode(tmp_path: Path) -> None:
    image = Image.new("RGB", (3000, 3000), "white")
    path = tmp_path / "large.png"
    image.save(path, format="PNG")
    config = Settings(visual_evidence_max_image_pixels=1_000_000)
    with pytest.raises(VisualEvidenceLimitError, match="image_pixel_limit_exceeded"):
        render_selected_pages(
            path,
            content_type="image/png",
            selected_page_indexes=[0],
            config=config,
        )


def test_model_reply_is_strict_and_never_expresses_human_absence() -> None:
    reply = VisualEvidenceModelReply.model_validate(
        {
            "outcome": "candidate_prompt_only",
            "visual_regions": [
                {
                    "page_index": 0,
                    "visual_bbox": [0, 0, 1, 1],
                    "evidence_type": "printed_prompt",
                }
            ],
        }
    )
    assert reply.outcome == "candidate_prompt_only"
    assert (
        "response_absent"
        not in VisualEvidenceModelReply.model_fields["outcome"].annotation.__args__
    )
    with pytest.raises(ValidationError):
        VisualEvidenceModelReply.model_validate(
            {"outcome": "response_absent", "visual_regions": []}
        )
    with pytest.raises(ValidationError):
        VisualEvidenceModelReply.model_validate(
            {
                "outcome": "candidate_prompt_only",
                "visual_regions": [
                    {
                        "page_index": 0,
                        "visual_bbox": [0, 0, 1, 1],
                        "evidence_type": "printed_prompt",
                        "question_id": str(uuid4()),
                    }
                ],
            }
        )


def test_printed_prompt_region_cannot_claim_handwriting_evidence() -> None:
    with pytest.raises(ValidationError, match="cannot claim student-work evidence"):
        VisualEvidenceModelRegion(
            page_index=0,
            visual_bbox=(0.1, 0.1, 0.8, 0.4),
            evidence_type="printed_prompt",
            reason_codes=["handwriting_visible"],
        )


@pytest.mark.parametrize(
    "bbox",
    [
        [-0.1, 0.1, 0.5, 0.6],
        [0.1, 0.1, 1.1, 0.6],
        [0.2, 0.2, 0.2, 0.3],
        [0.1, 0.1, "0.8", 0.8],
        [0.1, 0.1, float("nan"), 0.8],
    ],
)
def test_invalid_or_non_numeric_model_bbox_is_rejected(bbox) -> None:
    with pytest.raises(ValidationError):
        VisualEvidenceModelRegion(
            page_index=0,
            visual_bbox=bbox,
            evidence_type="handwritten_text",
        )


def test_forged_canonical_pointer_is_rejected_at_provider_boundary() -> None:
    with pytest.raises(ValidationError):
        VisualEvidenceModelRegion.model_validate(
            {
                "page_index": 0,
                "visual_bbox": [0.1, 0.1, 0.3, 0.3],
                "evidence_type": "handwritten_text",
                "candidate_content_pointer": "/pages/0/blocks/9/content",
            }
        )


def test_visual_bbox_only_becomes_candidate_with_one_owned_canonical_region() -> None:
    canonical, ids = _canonical()
    question_one, question_two = uuid4(), uuid4()
    first_pointer = "/pages/0/blocks/0/content/children/0"
    second_pointer = "/pages/0/blocks/0/content/children/1"
    first = AlignedAnswer(
        question_id=question_one,
        question_number=1,
        question_type="short_answer",
        matching_status="aligned",
        source_regions=[
            _source_region(first_pointer, ids.block_id, (0.1, 0.1, 0.45, 0.45))
        ],
    )
    second = AlignedAnswer(
        question_id=question_two,
        question_number=2,
        question_type="short_answer",
        matching_status="aligned",
        source_regions=[
            _source_region(second_pointer, ids.block_id, (0.55, 0.55, 0.9, 0.9))
        ],
    )
    reply = VisualEvidenceModelReply.model_validate_json(_good_reply())

    region = _attribute_regions(
        reply, first, [first, second], canonical, {0: _image_page()}
    )[0]
    assert region.attribution_status == "candidate"
    assert region.candidate_content_pointer == first_pointer
    assert region.candidate_source_block_id == ids.block_id
    assert "visual_evidence_candidate_only" in region.reason_codes


def test_region_for_another_questions_content_remains_unattributed() -> None:
    canonical, ids = _canonical()
    question_one, question_two = uuid4(), uuid4()
    first = AlignedAnswer(
        question_id=question_one,
        question_number=1,
        question_type="short_answer",
        matching_status="aligned",
        source_regions=[
            _source_region(
                "/pages/0/blocks/0/content/children/0", ids.block_id, (0.1, 0.1, 0.45, 0.45)
            )
        ],
    )
    second = AlignedAnswer(
        question_id=question_two,
        question_number=2,
        question_type="short_answer",
        matching_status="aligned",
        source_regions=[
            _source_region(
                "/pages/0/blocks/0/content/children/1", ids.block_id, (0.55, 0.55, 0.9, 0.9)
            )
        ],
    )
    wrong_question_region = VisualEvidenceModelReply(
        outcome="candidate_response_present",
        visual_regions=[
            VisualEvidenceModelRegion(
                page_index=0,
                visual_bbox=(0.6, 0.6, 0.85, 0.85),
                evidence_type="handwritten_text",
            )
        ],
    )
    region = _attribute_regions(
        wrong_question_region, first, [first, second], canonical, {0: _image_page()}
    )[0]
    assert region.attribution_status == "unresolved"
    assert region.candidate_content_pointer is None
    assert "question_attribution_uncertain" in region.reason_codes


def test_shared_image_asset_is_not_assigned_to_one_question() -> None:
    canonical, ids = _canonical(with_shared_image=True)
    question_one, question_two = uuid4(), uuid4()
    shared_ref = CanonicalAssetReference(kind="stored", asset_id=ids.image_id)
    first = AlignedAnswer(
        question_id=question_one,
        question_number=1,
        question_type="short_answer",
        matching_status="aligned",
        source_regions=[
            _source_region(
                "/pages/0/blocks/0/content/children/0",
                ids.block_id,
                (0.1, 0.1, 0.45, 0.45),
                asset_refs=[shared_ref],
            )
        ],
    )
    second = AlignedAnswer(
        question_id=question_two,
        question_number=2,
        question_type="short_answer",
        matching_status="aligned",
        source_regions=[
            _source_region(
                "/pages/0/blocks/0/content/children/1",
                ids.block_id,
                (0.1, 0.1, 0.45, 0.45),
                asset_refs=[shared_ref],
            )
        ],
    )
    reply = VisualEvidenceModelReply(
        outcome="candidate_response_present",
        visual_regions=[
            VisualEvidenceModelRegion(
                page_index=0,
                visual_bbox=(0.12, 0.12, 0.4, 0.42),
                evidence_type="geometry_mark",
            )
        ],
    )
    region = _attribute_regions(reply, first, [first, second], canonical, {0: _image_page()})[0]
    assert region.attribution_status == "unresolved"
    assert region.candidate_content_pointer is None
    assert "shared_figure_possible" in region.reason_codes


def test_candidate_regions_do_not_overlap_by_question_or_coordinates() -> None:
    assert _boxes_match((0.1, 0.1, 0.4, 0.4), (0.1, 0.1, 0.4, 0.4))
    assert not _boxes_match((0.1, 0.1, 0.2, 0.2), (0.8, 0.8, 0.9, 0.9))


def test_disabled_provider_and_unapproved_remote_endpoint_fail_closed() -> None:
    with pytest.raises(VisualEvidenceConfigurationError, match="visual_model_disabled"):
        OpenAICompatibleVisionProvider(Settings(visual_evidence_enabled=False))
    with pytest.raises(VisualEvidenceConfigurationError, match="model_endpoint_not_permitted"):
        OpenAICompatibleVisionProvider(
            Settings(
                visual_evidence_enabled=True,
                visual_evidence_base_url="http://169.254.169.254/v1",
            )
        )
    permitted = OpenAICompatibleVisionProvider(
        Settings(
            visual_evidence_enabled=True,
            visual_evidence_base_url="https://vlm.example.test/v1",
            visual_evidence_allow_remote=True,
            visual_evidence_external_data_authorized=True,
        ),
        transport=httpx.MockTransport(lambda request: _response(_good_reply())),
    )
    assert permitted.model_id


@pytest.mark.anyio
async def test_ollama_native_provider_disables_thinking_and_validates_json_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "message": {"content": _good_reply(), "thinking": "private reasoning"},
                "prompt_eval_count": 80,
                "eval_count": 45,
                "done_reason": "stop",
            },
        )

    config = Settings(
        visual_evidence_enabled=True,
        visual_evidence_provider="ollama_native",
        visual_evidence_base_url="http://127.0.0.1:11434/v1",
    )
    provider = OllamaNativeVisionProvider(config, transport=httpx.MockTransport(handler))
    response = await provider.analyze(
        question_number=1,
        question_stem="Solve 2 + 2",
        source_regions=[],
        pages=[_image_page()],
    )

    request_body = captured["body"]
    assert captured["path"] == "/api/chat"
    assert request_body["think"] is False
    assert request_body["format"] == "json"
    assert request_body["stream"] is False
    assert len(request_body["messages"][1]["images"]) == 1
    assert response.reply.outcome == "candidate_response_present"
    assert response.usage is not None
    assert response.usage.model_dump() == {
        "prompt_tokens": 80,
        "completion_tokens": 45,
        "total_tokens": 125,
    }

    with pytest.raises(
        VisualEvidenceConfigurationError,
        match="ollama_native_endpoint_must_be_local",
    ):
        OllamaNativeVisionProvider(
            Settings(
                visual_evidence_enabled=True,
                visual_evidence_base_url="https://vlm.example.test/v1",
                visual_evidence_allow_remote=True,
                visual_evidence_external_data_authorized=True,
            )
        )


@pytest.mark.anyio
async def test_provider_uses_bounded_retry_and_reports_provider_usage() -> None:
    attempts = 0
    captured_request = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts, captured_request
        attempts += 1
        captured_request = json.loads(request.content)
        if attempts == 1:
            return httpx.Response(503)
        return _response(
            _good_reply(),
            usage={"prompt_tokens": 420, "completion_tokens": 45, "total_tokens": 465},
        )

    provider = OpenAICompatibleVisionProvider(
        Settings(visual_evidence_enabled=True, visual_evidence_max_retries=1),
        transport=httpx.MockTransport(handler),
    )
    response = await provider.analyze(
        question_number=1,
        question_stem="Solve 2 + 2",
        source_regions=[AlignmentRegionSummary(0, (0.1, 0.1, 0.8, 0.8), "text")],
        pages=[_image_page()],
    )
    assert isinstance(response, VisionProviderResponse)
    assert response.reply.outcome == "candidate_response_present"
    assert response.elapsed_ms >= 0
    assert response.usage is not None and response.usage.total_tokens == 465
    assert attempts == 2
    prompt = json.dumps(captured_request, ensure_ascii=False)
    assert "question_id" not in prompt
    assert "content_pointer" not in prompt


@pytest.mark.anyio
async def test_provider_does_not_retry_invalid_model_json() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _response("{not-json")

    provider = OpenAICompatibleVisionProvider(
        Settings(visual_evidence_enabled=True, visual_evidence_max_retries=2),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(VisualEvidenceProtocolError, match="model_json_contract_invalid"):
        await provider.analyze(
            question_number=1,
            question_stem="Question",
            source_regions=[],
            pages=[_image_page()],
        )
    assert attempts == 1


@pytest.mark.anyio
async def test_provider_retries_transient_timeout_only_within_configured_bound() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("test timeout", request=request)

    provider = OpenAICompatibleVisionProvider(
        Settings(
            visual_evidence_enabled=True,
            visual_evidence_max_retries=1,
            visual_evidence_timeout_seconds=0.1,
        ),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(VisualEvidenceProviderError, match="model_request_failed_after_retries"):
        await provider.analyze(
            question_number=1,
            question_stem="Question",
            source_regions=[],
            pages=[_image_page()],
        )
    assert attempts == 2
