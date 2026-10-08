"""MinerU 4.x MiddleJson, asset, and resource-boundary contract tests."""

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

from huipi_cloud.infrastructure.parsing.child_guard import (
    _INVALID_PDF_EXIT,
    _PAGE_LIMIT_EXIT,
    _validate_pdf,
)
from huipi_cloud.infrastructure.parsing.mineru import _sha256_file, _validate_archive
from huipi_cloud.modules.parsing.errors import PermanentParsingError


def _middle_json(*, version: str = "2.0") -> dict[str, object]:
    return {
        "schema": "docvortex.middle",
        "schema_version": version,
        "metadata": {
            "producer": {"name": "mineru", "version": "4.0.10"},
        },
        "extensions": {"mineru": {"tier": "basic", "parse_mode": "txt"}},
        "pages": [{"page_idx": 0, "blocks": [{"type": "text", "index": 0}]}],
        "is_full_document": True,
    }


def _write_bundle(
    archive_path: Path,
    *,
    markdown: str = "test document",
    middle: dict[str, object] | None = None,
    assets: dict[str, bytes] | None = None,
) -> None:
    middle_value = middle or _middle_json()
    structured = {"pages": middle_value["pages"], "metadata": middle_value["metadata"]}
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("markdown.md", markdown)
        archive.writestr("middle_json.json", json.dumps(middle_value))
        archive.writestr("structured_content.json", json.dumps(structured))
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


def test_missing_referenced_image_asset_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "missing-image.zip"
    _write_bundle(archive, markdown="![question](images/page_0_image_0.png)")

    with pytest.raises(PermanentParsingError, match="invalid_result"):
        _validate_archive(archive, tmp_path, "basic", 1024 * 1024, 1024 * 1024, 100)


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
