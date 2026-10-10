"""Bounded page rendering for PDF and raster submission files."""

from __future__ import annotations

import hashlib
import math
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader

from huipi_cloud.core.config import Settings, settings
from huipi_cloud.modules.visual_evidence.errors import (
    VisualEvidenceError,
    VisualEvidenceInputError,
    VisualEvidenceLimitError,
)


@dataclass(frozen=True)
class RenderedPage:
    """One selected page image plus an auditable rendering transform."""

    page_index: int
    image_bytes: bytes
    content_type: str
    source_width: float
    source_height: float
    source_unit: str
    source_rotation_degrees: int
    rendered_width: int
    rendered_height: int
    effective_dpi: float | None
    exif_orientation: int | None
    mapping_eligible: bool
    mapping_reason: str | None
    transform: tuple[float, float, float, float, float, float]
    sha256: str


def render_selected_pages(
    path: Path | bytes,
    *,
    content_type: str,
    selected_page_indexes: list[int],
    config: Settings = settings,
) -> list[RenderedPage]:
    """Render only aligned source pages; indexes are zero-based Canonical indexes."""
    if not selected_page_indexes or len(set(selected_page_indexes)) != len(selected_page_indexes):
        raise VisualEvidenceInputError("selected_pages_invalid")
    if len(selected_page_indexes) > config.visual_evidence_max_pages_per_question:
        raise VisualEvidenceLimitError("too_many_question_pages")
    if content_type == "application/pdf":
        return _render_pdf(path, selected_page_indexes, config)
    if content_type in {"image/jpeg", "image/png"}:
        if selected_page_indexes != [0]:
            raise VisualEvidenceInputError("image_page_index_invalid")
        return [_render_image(path, content_type, config)]
    raise VisualEvidenceInputError("unsupported_source_content_type")


def _render_pdf(path: Path | bytes, selected: list[int], config: Settings) -> list[RenderedPage]:
    try:
        reader_input = BytesIO(path) if isinstance(path, bytes) else path
        reader = PdfReader(reader_input, strict=True)
        if reader.is_encrypted:
            raise VisualEvidenceInputError("encrypted_pdf_unsupported")
        page_count = len(reader.pages)
        if page_count < 1 or page_count > min(
            config.mineru_max_pdf_pages, config.visual_evidence_max_source_pdf_pages
        ):
            raise VisualEvidenceLimitError("pdf_page_count_limit_exceeded")
        if any(index < 0 or index >= page_count for index in selected):
            raise VisualEvidenceInputError("selected_pdf_page_not_found")
        document = pdfium.PdfDocument(path if isinstance(path, bytes) else str(path))
    except VisualEvidenceError:
        raise
    except Exception as error:
        raise VisualEvidenceInputError("pdf_invalid_or_unsupported") from error

    try:
        if len(document) != page_count:
            raise VisualEvidenceInputError("pdf_page_count_mismatch")
        rendered: list[RenderedPage] = []
        for index in sorted(selected):
            try:
                source_page = reader.pages[index]
                media_width = float(source_page.mediabox.width)
                media_height = float(source_page.mediabox.height)
                rotation = int(source_page.rotation or 0) % 360
                page = document[index]
                display_width, display_height = page.get_size()
                if min(display_width, display_height, media_width, media_height) <= 0:
                    raise VisualEvidenceInputError("pdf_page_dimensions_invalid")
                scale = _bounded_scale(
                    float(display_width),
                    float(display_height),
                    config.visual_evidence_render_dpi / 72,
                    config.visual_evidence_max_image_pixels,
                    config.visual_evidence_max_render_edge,
                )
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil().convert("RGB")
                image = _resize_to_edge(image, config.visual_evidence_max_render_edge)
                image_bytes = _encode_bounded_jpeg(
                    image, config.visual_evidence_max_page_image_bytes
                )
                width, height = image.size
                rendered.append(
                    RenderedPage(
                        page_index=index,
                        image_bytes=image_bytes,
                        content_type="image/jpeg",
                        source_width=media_width,
                        source_height=media_height,
                        source_unit="pdf_points",
                        source_rotation_degrees=rotation,
                        rendered_width=width,
                        rendered_height=height,
                        effective_dpi=width / float(display_width) * 72.0,
                        exif_orientation=None,
                        mapping_eligible=rotation == 0,
                        mapping_reason=None if rotation == 0 else "rotated_pdf_page",
                        transform=_rotation_display_to_source(rotation),
                        sha256=hashlib.sha256(image_bytes).hexdigest(),
                    )
                )
            except VisualEvidenceError:
                raise
            except Exception as error:
                raise VisualEvidenceInputError("pdf_page_render_failed") from error
        return rendered
    finally:
        document.close()


def _render_image(path: Path | bytes, content_type: str, config: Settings) -> RenderedPage:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image_source = BytesIO(path) if isinstance(path, bytes) else path
            with Image.open(image_source) as source:
                expected_format = "JPEG" if content_type == "image/jpeg" else "PNG"
                if source.format != expected_format:
                    raise VisualEvidenceInputError("image_content_type_mismatch")
                raw_width, raw_height = source.size
                _check_pixel_count(raw_width, raw_height, config.visual_evidence_max_image_pixels)
                orientation = source.getexif().get(274, 1)
                if not isinstance(orientation, int) or not 1 <= orientation <= 8:
                    raise VisualEvidenceInputError("image_exif_orientation_invalid")
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGB")
    except VisualEvidenceError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise VisualEvidenceInputError("image_invalid_or_unsafe") from error

    image = _resize_to_edge(image, config.visual_evidence_max_render_edge)
    image_bytes = _encode_bounded_jpeg(image, config.visual_evidence_max_page_image_bytes)
    width, height = image.size
    return RenderedPage(
        page_index=0,
        image_bytes=image_bytes,
        content_type="image/jpeg",
        source_width=float(raw_width),
        source_height=float(raw_height),
        source_unit="image_pixels",
        source_rotation_degrees=0,
        rendered_width=width,
        rendered_height=height,
        effective_dpi=None,
        exif_orientation=orientation,
        mapping_eligible=orientation == 1,
        mapping_reason=None if orientation == 1 else "exif_orientation_transformed",
        transform=_exif_display_to_source(orientation),
        sha256=hashlib.sha256(image_bytes).hexdigest(),
    )


def _bounded_scale(
    width: float,
    height: float,
    desired: float,
    max_pixels: int,
    max_edge: int,
) -> float:
    scale = min(desired, max_edge / max(width, height))
    if width * height * scale * scale > max_pixels:
        scale = min(scale, (max_pixels / (width * height)) ** 0.5)
    if scale <= 0:
        raise VisualEvidenceLimitError("render_scale_invalid")
    return scale


def _check_pixel_count(width: int, height: int, maximum: int) -> None:
    if width < 1 or height < 1 or width * height > maximum:
        raise VisualEvidenceLimitError("image_pixel_limit_exceeded")


def _resize_to_edge(image: Image.Image, edge: int) -> Image.Image:
    if max(image.size) <= edge:
        return image
    scale = edge / max(image.size)
    return image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.Resampling.LANCZOS,
    )


def _encode_bounded_jpeg(image: Image.Image, maximum: int) -> bytes:
    current = image
    for quality in (88, 80, 72, 64, 56):
        buffer = BytesIO()
        current.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() <= maximum:
            return buffer.getvalue()
    while max(current.size) > 512:
        current = current.resize(
            (max(1, round(current.width * 0.8)), max(1, round(current.height * 0.8))),
            Image.Resampling.LANCZOS,
        )
        buffer = BytesIO()
        current.save(buffer, format="JPEG", quality=56, optimize=True)
        if buffer.tell() <= maximum:
            return buffer.getvalue()
    raise VisualEvidenceLimitError("rendered_page_byte_limit_exceeded")


def _rotation_display_to_source(rotation: int) -> tuple[float, float, float, float, float, float]:
    # Affine map from rendered top-left normalized coordinates back to raw page coordinates.
    return {
        0: (1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
        90: (0.0, 1.0, 0.0, -1.0, 0.0, 1.0),
        180: (-1.0, 0.0, 1.0, 0.0, -1.0, 1.0),
        270: (0.0, -1.0, 1.0, 1.0, 0.0, 0.0),
    }.get(rotation, (1.0, 0.0, 0.0, 0.0, 1.0, 0.0))


def _exif_display_to_source(orientation: int) -> tuple[float, float, float, float, float, float]:
    # Affine map from EXIF-corrected, normalized raster coordinates to stored-file pixels.
    return {
        1: (1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
        2: (-1.0, 0.0, 1.0, 0.0, 1.0, 0.0),
        3: (-1.0, 0.0, 1.0, 0.0, -1.0, 1.0),
        4: (1.0, 0.0, 0.0, 0.0, -1.0, 1.0),
        5: (0.0, 1.0, 0.0, 1.0, 0.0, 0.0),
        6: (0.0, 1.0, 0.0, -1.0, 0.0, 1.0),
        7: (0.0, -1.0, 1.0, -1.0, 0.0, 1.0),
        8: (0.0, -1.0, 1.0, 1.0, 0.0, 0.0),
    }[orientation]


def transform_bbox_to_source(
    bbox: tuple[float, float, float, float],
    transform: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float]:
    """Apply an auditable affine map from rendered normalized page to source page."""
    x0, y0, x1, y1 = bbox
    a, b, c, d, e, f = transform
    points = (
        (a * x0 + b * y0 + c, d * x0 + e * y0 + f),
        (a * x0 + b * y1 + c, d * x0 + e * y1 + f),
        (a * x1 + b * y0 + c, d * x1 + e * y0 + f),
        (a * x1 + b * y1 + c, d * x1 + e * y1 + f),
    )
    if any(not math.isfinite(value) or not 0 <= value <= 1 for point in points for value in point):
        raise VisualEvidenceInputError("bbox_transform_out_of_bounds")
    result = (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )
    if result[2] <= result[0] or result[3] <= result[1]:
        raise VisualEvidenceInputError("bbox_transform_degenerate")
    return result
