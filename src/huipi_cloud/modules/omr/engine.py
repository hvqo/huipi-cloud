"""Bounded OMR for our A4 sheets. Never infer a student identity from a page.

The layout and registration code are server-generated and immutable. Inputs are
the already size-limited, SHA-verified scan previews, not caller-supplied images.
"""

import re
from hashlib import sha256
from io import BytesIO

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

WIDTH, HEIGHT = 1240, 1754
ANCHORS = ((72, 72), (1168, 72), (72, 1682), (1168, 1682))
MAX_QUESTIONS = 30
OPTIONS = "ABCD"
ENGINE_VERSION = "a4-four-choice-v1"


class SheetError(ValueError):
    """The sheet cannot be safely aligned or does not match this template."""


def token_bits(template_id: str) -> list[int]:
    return [
        int(bit)
        for byte in sha256(template_id.encode("ascii")).digest()[:6]
        for bit in f"{byte:08b}"
    ]


def bit_center(index: int) -> tuple[int, int]:
    return (190 + (index % 24) * 36, 150 + (index // 24) * 38)


def bubble_center(index: int, option: int) -> tuple[int, int]:
    column, row = divmod(index, 15)
    return (245 + column * 550 + option * 65, 325 + row * 86)


def _font(size: int):
    # DejaVu is bundled with Pillow on supported installations; fallback is legible ASCII.
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def render_image(template_id: str, labels: list[str]) -> Image.Image:
    if not 1 <= len(labels) <= MAX_QUESTIONS:
        raise SheetError("question_count_invalid")
    if any(re.fullmatch(r"[A-Za-z0-9._()\-]{1,12}", label) is None for label in labels):
        raise SheetError("printed_label_unsupported")
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    d = ImageDraw.Draw(image)
    for x, y in ANCHORS:
        d.rectangle((x - 24, y - 24, x + 24, y + 24), fill="black")
    d.text((94, 92), "HUIPI - ANSWER SHEET", font=_font(29), fill="black")
    d.text((690, 100), f"FORM {template_id[:12]}", font=_font(20), fill="black")
    d.text(
        (100, 229),
        "Name: __________________   ID: __________________",
        font=_font(24),
        fill="black",
    )
    d.text(
        (100, 263),
        "Fill one circle completely. Identity is checked separately.",
        font=_font(18),
        fill="black",
    )
    for i, bit in enumerate(token_bits(template_id)):
        x, y = bit_center(i)
        d.rectangle(
            (x - 10, y - 10, x + 10, y + 10),
            outline="black",
            width=2,
            fill="black" if bit else "white",
        )
    for i, label in enumerate(labels):
        x, y = bubble_center(i, 0)
        d.text((x - 125, y - 15), f"{label}.", font=_font(25), fill="black")
        for o, letter in enumerate(OPTIONS):
            x, y = bubble_center(i, o)
            d.ellipse((x - 20, y - 20, x + 20, y + 20), outline="black", width=3)
            d.text((x - 7, y - 12), letter, font=_font(18), fill="black")
    return image


def render_pdf(template_id: str, labels: list[str]) -> bytes:
    image = render_image(template_id, labels)
    out = BytesIO()
    try:
        image.save(out, "PDF", resolution=150.0)
        return out.getvalue()
    finally:
        image.close()


def align_page(data: bytes, template_id: str, *, overlay: bool = False):
    if not data or len(data) > 2 * 1024 * 1024:
        raise SheetError("image_size_invalid")
    try:
        with Image.open(BytesIO(data)) as source:
            if (
                source.width > 1600
                or source.height > 1600
                or source.width < 500
                or source.height < 700
            ):
                raise SheetError("image_dimensions_invalid")
            source.load()
            rgb = np.asarray(source.convert("RGB"))
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise SheetError("invalid_image") from exc
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    _, mask = cv2.threshold(gray, 110, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = gray.shape
    candidates = []
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        if (
            0.00012 * w * h < area < 0.004 * w * h
            and 0.65 < cw / ch < 1.5
            and area / (cw * ch) > 0.68
        ):
            candidates.append((x + cw / 2, y + ch / 2))
    points = []
    for ax, ay in ANCHORS:
        ex, ey = ax / WIDTH * w, ay / HEIGHT * h
        near = [(x, y) for x, y in candidates if abs(x - ex) < w * 0.14 and abs(y - ey) < h * 0.14]
        if not near:
            raise SheetError("alignment_markers_missing")
        points.append(min(near, key=lambda p: (p[0] - ex) ** 2 + (p[1] - ey) ** 2))
    if len(set(points)) != 4:
        raise SheetError("alignment_markers_ambiguous")
    polygon = np.float32([points[0], points[1], points[3], points[2]])
    if not cv2.isContourConvex(polygon) or cv2.contourArea(polygon) < w * h * 0.4:
        raise SheetError("alignment_markers_invalid")
    transform = cv2.getPerspectiveTransform(np.float32(points), np.float32(ANCHORS))
    if not np.isfinite(transform).all() or abs(float(np.linalg.det(transform))) < 1e-10:
        raise SheetError("alignment_markers_invalid")
    warped = cv2.warpPerspective(gray, transform, (WIDTH, HEIGHT), borderValue=255)
    sharpness = (
        sum(
            cv2.Laplacian(warped[y - 35 : y + 35, x - 35 : x + 35], cv2.CV_64F).var()
            for x, y in ANCHORS
        )
        / 4
    )
    if sharpness < 40:
        raise SheetError("page_blurry")
    actual_bits = []
    for i in range(48):
        x, y = bit_center(i)
        ratio = float(np.mean(warped[y - 6 : y + 6, x - 6 : x + 6] < 125))
        if 0.20 < ratio < 0.70:
            raise SheetError("template_code_unclear")
        actual_bits.append(int(ratio >= 0.70))
    if actual_bits != token_bits(template_id):
        raise SheetError("template_code_mismatch")
    if overlay:
        return warped, transform, rgb
    return warped


def recognize(data: bytes, template_id: str, labels: list[str]) -> list[dict]:
    if not 1 <= len(labels) <= MAX_QUESTIONS:
        raise SheetError("question_count_invalid")
    page = align_page(data, template_id)
    results = []
    yy, xx = np.ogrid[-12:13, -12:13]
    for i, label in enumerate(labels):
        ratios, weak_ratios = [], []
        for option in range(4):
            x, y = bubble_center(i, option)
            region = page[y - 12 : y + 13, x - 12 : x + 13]
            # Printed option letters occupy the bubble centre. Erase their known baseline
            # by measuring the outer interior ring, excluding the printed outline and glyph.
            ring = (xx * xx + yy * yy <= 12 * 12) & (xx * xx + yy * yy >= 8 * 8)
            ratios.append(float(np.mean(region[ring] < 125)))
            weak_ratios.append(float(np.mean(region[ring] < 240)))
        marked = [j for j, ratio in enumerate(ratios) if ratio >= 0.55]
        uncertain = any(
            ratio < 0.55 and (ratio > 0.12 or weak > 0.22)
            for ratio, weak in zip(ratios, weak_ratios, strict=True)
        )
        status = (
            "multiple"
            if len(marked) > 1
            else "uncertain"
            if uncertain
            else "single"
            if marked
            else "blank"
        )
        results.append(
            dict(
                label=label,
                status=status,
                choice=OPTIONS[marked[0]] if status == "single" else None,
                ratios=[round(x, 4) for x in ratios],
            )
        )
    return results


def evidence_overlay(data: bytes, template_id: str, labels: list[str]) -> bytes:
    """Project server-owned bubble locations back onto the original scan preview."""
    if not 1 <= len(labels) <= MAX_QUESTIONS:
        raise SheetError("question_count_invalid")
    _, transform, rgb = align_page(data, template_id, overlay=True)
    output = cv2.cvtColor(rgb.copy(), cv2.COLOR_RGB2BGR)
    inverse = np.linalg.inv(transform)
    for i, _ in enumerate(labels):
        for option in range(4):
            x, y = bubble_center(i, option)
            points = np.float32(
                [[[x - 25, y - 25], [x + 25, y - 25], [x + 25, y + 25], [x - 25, y + 25]]]
            )
            quad = cv2.perspectiveTransform(points, inverse).astype(np.int32)
            cv2.polylines(output, [quad], True, (0, 128, 255), 2, cv2.LINE_AA)
    ok, result = cv2.imencode(".jpg", output, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok or len(result) > 2 * 1024 * 1024:
        raise SheetError("overlay_unavailable")
    return result.tobytes()
