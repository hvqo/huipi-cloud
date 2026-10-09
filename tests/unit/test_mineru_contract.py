"""MinerU 4.x MiddleJson, asset, and resource-boundary contract tests."""

import hashlib
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from pypdf import PdfWriter

from huipi_cloud.infrastructure.parsing.child_guard import (
    _INVALID_PDF_EXIT,
    _PAGE_LIMIT_EXIT,
    _validate_pdf,
)
from huipi_cloud.infrastructure.parsing.mineru import (
    MinerUParserExecutor,
    _sha256_file,
    _validate_archive,
)
from huipi_cloud.modules.parsing.errors import PermanentParsingError
from huipi_cloud.modules.parsing.executor import ParsingInput


def _middle_json(*, version: str = "2.0") -> dict[str, object]:
    return {
        "schema": "docvortex.middle",
        "schema_version": version,
        "metadata": {
            "file_suffix": "pdf",
            "producer": {"name": "mineru", "version": "4.0.10"},
            "document": {},
        },
        "extensions": {"mineru": {"tier": "basic", "parse_mode": "txt"}},
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "text",
                        "index": 0,
                        "bbox": [0.1, 0.1, 0.9, 0.2],
                        "content": [{"type": "text", "content": "sample"}],
                    }
                ],
            }
        ],
        "is_full_document": True,
    }


def _write_bundle(
    archive_path: Path,
    *,
    markdown: str = "test document",
    middle: object | None = None,
    structured: object | None = None,
    assets: dict[str, bytes] | None = None,
) -> None:
    middle_value = _middle_json() if middle is None else middle
    assert isinstance(middle_value, dict)
    if structured is None:
        structured_value = {
            "pages": middle_value["pages"],
            "metadata": middle_value["metadata"],
            "extensions": middle_value["extensions"],
            "is_full_document": middle_value["is_full_document"],
        }
    else:
        structured_value = structured
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("markdown.md", markdown)
        archive.writestr("middle_json.json", json.dumps(middle_value))
        archive.writestr("structured_content.json", json.dumps(structured_value))
        for name, payload in (assets or {}).items():
            archive.writestr(name, payload)


def test_valid_middle_json_keeps_original_zero_based_page_indexes(tmp_path: Path) -> None:
    archive = tmp_path / "result.zip"
    _write_bundle(archive)

    result = _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)

    assert result.schema_name == "docvortex.middle"
    assert result.schema_version == "2.0"
    assert result.page_count == 1
    assert result.parser_version == "4.0.10"
    parsed = json.loads(result.middle_json)
    assert parsed["pages"][0]["page_idx"] == 0


def test_unsupported_middle_json_schema_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "unsupported.zip"
    _write_bundle(archive, middle=_middle_json(version="1.0"))

    with pytest.raises(PermanentParsingError, match="invalid_result"):
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda middle: middle["metadata"].update(producer="mineru"),
        lambda middle: middle["metadata"].update(producer=None),
        lambda middle: middle.update(metadata="invalid"),
        lambda middle: middle.update(metadata=None),
        lambda middle: middle.update(pages=[None]),
        lambda middle: middle["pages"][0].update(blocks=[None]),
        lambda middle: middle["pages"][0]["blocks"][0].update(content=[None]),
        lambda middle: middle["pages"][0]["blocks"][0].update(type=[]),
    ],
    ids=[
        "producer-string",
        "producer-null",
        "metadata-string",
        "metadata-null",
        "page-null",
        "block-null",
        "nested-block-null",
        "block-type-array",
    ],
)
def test_malformed_middle_json_is_a_safe_permanent_failure(
    tmp_path: Path,
    mutation,
) -> None:
    middle = _middle_json()
    mutation(middle)
    archive = tmp_path / "malformed-middle.zip"
    _write_bundle(archive, middle=middle)

    with pytest.raises(PermanentParsingError, match="invalid_result") as raised:
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)

    assert str(raised.value) == "invalid_result"
    assert "producer" not in str(raised.value)
    assert not raised.value.summary.retryable


def test_structured_page_source_indexes_must_match_middle_json(tmp_path: Path) -> None:
    middle = _middle_json()
    structured = {
        "pages": [{"page_idx": 1, "blocks": [{"type": "text"}]}],
        "metadata": middle["metadata"],
    }
    archive = tmp_path / "mismatched-pages.zip"
    _write_bundle(archive, middle=middle, structured=structured)

    with pytest.raises(PermanentParsingError, match="invalid_result"):
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)


def test_structured_page_block_shape_must_match_middle_json(tmp_path: Path) -> None:
    middle = _middle_json()
    structured = {
        "pages": [{"page_idx": 0, "blocks": ["not a block"]}],
        "metadata": middle["metadata"],
    }
    archive = tmp_path / "invalid-structured-block.zip"
    _write_bundle(archive, middle=middle, structured=structured)

    with pytest.raises(PermanentParsingError, match="invalid_result"):
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)


def test_missing_referenced_image_asset_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "missing-image.zip"
    _write_bundle(archive, markdown="![question](images/page_0_image_0.png)")

    with pytest.raises(PermanentParsingError, match="invalid_result"):
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)


def test_missing_non_images_relative_image_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "missing-relative-image.zip"
    _write_bundle(archive, markdown="![question](figures/question.png)")

    with pytest.raises(PermanentParsingError, match="invalid_result"):
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)


def test_missing_json_image_sidecar_is_rejected(tmp_path: Path) -> None:
    middle = _middle_json()
    middle["pages"][0]["blocks"][0]["image_path"] = "figures/question.png"
    archive = tmp_path / "missing-json-image.zip"
    _write_bundle(archive, middle=middle)

    with pytest.raises(PermanentParsingError, match="invalid_result"):
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)


def test_middle_json_image_path_resolves_to_valid_sidecar(tmp_path: Path) -> None:
    middle = _middle_json()
    bbox = [0.1, 0.1, 0.9, 0.9]
    middle["pages"][0]["blocks"] = [
        {
            "type": "image",
            "index": 0,
            "bbox": bbox,
            "content": [
                {
                    "type": "image_body",
                    "index": 0,
                    "bbox": bbox,
                    "content": "figure",
                    "image_path": "figures/figure.png",
                }
            ],
        }
    ]
    structured = {
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "image",
                        "bbox": bbox,
                        "content": "figure",
                        "image_source": "figures/figure.png",
                    }
                ],
            }
        ],
        "metadata": middle["metadata"],
        "extensions": middle["extensions"],
        "is_full_document": middle["is_full_document"],
    }
    archive = tmp_path / "middle-image.zip"
    _write_bundle(
        archive,
        middle=middle,
        structured=structured,
        assets={"figures/figure.png": b"PNG synthetic asset"},
    )

    result = _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)

    assert len(result.assets) == 1
    assert result.assets[0].name == "figures/figure.png"


def test_url_encoded_image_reference_resolves_to_safe_archive_asset(tmp_path: Path) -> None:
    archive = tmp_path / "encoded-image.zip"
    _write_bundle(
        archive,
        markdown="![question](figures/page%200.png)",
        assets={"figures/page 0.png": b"PNG synthetic asset"},
    )

    result = _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)

    assert len(result.assets) == 1
    assert result.assets[0].name == "figures/page 0.png"


@pytest.mark.parametrize(
    "reference",
    [
        "images/../outside.png",
        "images/%2e%2e/outside.png",
        "%2Foutside.png",
        "C%3A/outside.png",
    ],
)
def test_image_path_traversal_is_rejected(tmp_path: Path, reference: str) -> None:
    archive = tmp_path / "image-traversal.zip"
    _write_bundle(archive, markdown=f"![question]({reference})")

    with pytest.raises(PermanentParsingError, match="invalid_result"):
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)


def test_plain_markdown_link_is_not_treated_as_an_image_reference(tmp_path: Path) -> None:
    archive = tmp_path / "plain-link.zip"
    _write_bundle(archive, markdown="[question](figures/not-an-image.png)")

    result = _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)

    assert result.page_count == 1


def test_external_markdown_image_is_not_fetched_or_treated_as_local(tmp_path: Path) -> None:
    archive = tmp_path / "external-image.zip"
    _write_bundle(archive, markdown="![remote](https://example.invalid/figure.png)")

    result = _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)

    assert result.page_count == 1


def test_encoded_json_image_path_traversal_is_rejected(tmp_path: Path) -> None:
    middle = _middle_json()
    middle["pages"][0]["blocks"][0]["image_path"] = "images/%2e%2e/outside.png"
    archive = tmp_path / "json-image-traversal.zip"
    _write_bundle(archive, middle=middle)

    with pytest.raises(PermanentParsingError, match="invalid_result"):
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)


@pytest.mark.anyio
async def test_bucket_change_is_rejected_before_reading_old_submission() -> None:
    class BucketStorage:
        bucket = "bucket-b"

        def __init__(self) -> None:
            self.download_called = False

        async def download(self, object_key: str):
            self.download_called = True
            raise AssertionError(f"unexpected download: {object_key}")

    storage = BucketStorage()
    executor = object.__new__(MinerUParserExecutor)
    executor.storage = storage
    task = ParsingInput(
        task_id=uuid4(),
        submission_id=uuid4(),
        bucket="bucket-a",
        object_key="same/key.pdf",
        content_type="application/pdf",
        size_bytes=10,
        sha256="a" * 64,
        attempt_count=1,
    )

    with pytest.raises(PermanentParsingError, match="storage_bucket_mismatch") as raised:
        await executor.execute(task)

    assert not raised.value.summary.retryable
    assert not storage.download_called


def test_complete_image_asset_is_materialized_without_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "with-image.zip"
    _write_bundle(
        archive,
        markdown="![question](images/page_0_image_0.png)",
        assets={"images/page_0_image_0.png": b"PNG synthetic asset"},
    )

    result = _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)

    assert len(result.assets) == 1
    asset = result.assets[0]
    assert asset.path.read_bytes() == b"PNG synthetic asset"
    assert asset.sha256 == hashlib.sha256(b"PNG synthetic asset").hexdigest()
    assert asset.content_type == "image/png"
    assert asset.name == "images/page_0_image_0.png"
    assert asset.path.parent == tmp_path / "validated"


def test_zip_member_path_traversal_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "traversal.zip"
    _write_bundle(archive)
    with zipfile.ZipFile(archive, "a") as output:
        output.writestr("../outside.txt", b"not extracted")

    with pytest.raises(PermanentParsingError, match="invalid_result"):
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)


def test_archive_member_count_is_bounded(tmp_path: Path) -> None:
    archive = tmp_path / "many-members.zip"
    _write_bundle(archive, assets={f"images/{index}.png": b"x" for index in range(5)})

    with pytest.raises(PermanentParsingError, match="invalid_result"):
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 3)


def test_source_sha256_is_computed_incrementally(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic source bytes")

    digest, size = _sha256_file(source)

    assert digest == hashlib.sha256(b"synthetic source bytes").hexdigest()
    assert size == len(b"synthetic source bytes")


def test_child_pdf_preflight_rejects_corrupt_and_over_limit_inputs(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a PDF")
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    pdf = tmp_path / "two-pages.pdf"
    with pdf.open("wb") as output:
        writer.write(output)

    assert _validate_pdf(corrupt, 10) == _INVALID_PDF_EXIT
    assert _validate_pdf(pdf, 1) == _PAGE_LIMIT_EXIT
    assert _validate_pdf(pdf, 2) == 0
