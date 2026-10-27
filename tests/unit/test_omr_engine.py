"""Real OpenCV over synthetic forms, not recorded recognition fixtures."""

from io import BytesIO

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from huipi_cloud.modules.omr.engine import (
    SheetError,
    bubble_center,
    recognize,
    render_image,
    render_pdf,
)

TEMPLATE = "00000000-1111-2222-3333-444444444444"


def photo(marks=(), *, gray=0, perspective=False, blur=False):
    im = render_image(TEMPLATE, ["1", "2", "3", "4"])
    draw = ImageDraw.Draw(im)
    for number, option in marks:
        x, y = bubble_center(number, option)
        draw.ellipse((x - 16, y - 16, x + 16, y + 16), fill=(gray,) * 3)
    im.thumbnail((1600, 1600))
    if perspective:
        a = np.array(im)
        h, w = a.shape[:2]
        src = np.float32([[0, 0], [w, 0], [0, h], [w, h]])
        dst = np.float32([[35, 15], [w - 24, 42], [5, h - 30], [w - 10, h - 10]])
        a = cv2.warpPerspective(
            a, cv2.getPerspectiveTransform(src, dst), (w, h), borderValue=(255, 255, 255)
        )
        im = Image.fromarray(a)
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(2.5))
    result = BytesIO()
    im.save(result, "JPEG", quality=80)
    return result.getvalue()


def test_clear_blank_multiple_and_perspective():
    rows = recognize(
        photo([(0, 1), (1, 0), (1, 2)], perspective=True), TEMPLATE, ["1", "2", "3", "4"]
    )
    assert [(r["status"], r["choice"]) for r in rows] == [
        ("single", "B"),
        ("multiple", None),
        ("blank", None),
        ("blank", None),
    ]


def test_mismatched_template_is_rejected():
    with pytest.raises(SheetError, match="template_code_mismatch"):
        recognize(photo(), "00000000-1111-2222-3333-555555555555", ["1"])


def test_bad_or_unaligned_source_is_rejected():
    with pytest.raises(SheetError):
        recognize(b"not a page", TEMPLATE, ["1"])
    out = BytesIO()
    Image.new("RGB", (1000, 1400), "white").save(out, "JPEG")
    with pytest.raises(SheetError, match="alignment_markers_missing"):
        recognize(out.getvalue(), TEMPLATE, ["1"])


def test_faint_and_blurry_scans_never_become_clear_scores():
    value = recognize(photo([(0, 1)], gray=180), TEMPLATE, ["1"])[0]
    assert value["status"] == "uncertain" and value["choice"] is None
    value = recognize(photo([(0, 1)], gray=235), TEMPLATE, ["1"])[0]
    assert value["status"] == "uncertain" and value["choice"] is None
    with pytest.raises(SheetError, match="page_blurry"):
        recognize(photo([(0, 1)], blur=True), TEMPLATE, ["1"])


def test_printable_pdf_and_layout_bounds():
    assert render_pdf(TEMPLATE, ["1", "2"]).startswith(b"%PDF")
    with pytest.raises(SheetError, match="question_count_invalid"):
        render_image(TEMPLATE, [])
