"""Generated, visibly synthetic page images used by unit and local VLM checks."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def create_visual_samples(directory: Path) -> dict[str, Path]:
    """Create five fictional page samples; no student work or binary fixture is stored."""
    directory.mkdir(parents=True, exist_ok=True)
    samples: dict[str, Path] = {}
    for name in ("print_only", "mixed_handwriting", "formula", "geometry", "blank_region"):
        image = Image.new("RGB", (1000, 1400), "white")
        draw = ImageDraw.Draw(image)
        title_font = _font(42)
        body_font = _font(34)
        draw.text(
            (90, 90),
            "SYNTHETIC SAMPLE - NOT A REAL STUDENT PAPER",
            fill="#777777",
            font=_font(20),
        )
        draw.text(
            (90, 180),
            "Question 1: Solve 2 + 2. Show your work.",
            fill="black",
            font=title_font,
        )
        draw.rectangle((100, 350, 900, 1100), outline="#555555", width=3)
        draw.text((130, 380), "Answer area", fill="#555555", font=body_font)

        if name == "print_only":
            draw.text((150, 500), "Printed prompt only.", fill="black", font=body_font)
        elif name == "mixed_handwriting":
            _draw_handwritten_four(draw, 260, 560)
        elif name == "formula":
            _draw_handwritten_formula(draw, 180, 560)
        elif name == "geometry":
            _draw_geometry_marks(draw)
        elif name == "blank_region":
            draw.text((150, 500), "No marks in the answer area.", fill="#444444", font=body_font)

        path = directory / f"{name}.png"
        image.save(path, format="PNG", optimize=True)
        samples[name] = path
    return samples


def _draw_handwritten_four(draw: ImageDraw.ImageDraw, left: int, top: int) -> None:
    blue = "#174fc4"
    draw.line(
        [(left + 95, top), (left + 25, top + 100), (left + 140, top + 95)],
        fill=blue,
        width=8,
    )
    draw.line([(left + 115, top - 8), (left + 110, top + 155)], fill=blue, width=8)


def _draw_handwritten_formula(draw: ImageDraw.ImageDraw, left: int, top: int) -> None:
    blue = "#174fc4"
    draw.line([(left, top + 12), (left + 56, top + 92), (left + 110, top + 10)], fill=blue, width=8)
    draw.line([(left + 150, top + 45), (left + 225, top + 45)], fill=blue, width=7)
    draw.line([(left + 188, top + 15), (left + 188, top + 78)], fill=blue, width=7)
    draw.line(
        [(left + 270, top + 95), (left + 305, top + 35), (left + 340, top + 95)],
        fill=blue,
        width=8,
    )
    draw.text((left + 365, top + 24), "= 4", fill=blue, font=_font(74))


def _draw_geometry_marks(draw: ImageDraw.ImageDraw) -> None:
    blue = "#174fc4"
    draw.line([(250, 900), (500, 570), (760, 900), (250, 900)], fill="#222222", width=6)
    draw.line([(500, 570), (500, 900)], fill=blue, width=7)
    draw.ellipse((488, 705, 512, 729), outline=blue, width=5)
    draw.line([(475, 890), (500, 890), (500, 915)], fill=blue, width=5)
    draw.text((220, 910), "A", fill="black", font=_font(30))
    draw.text((490, 525), "B", fill="black", font=_font(30))
    draw.text((760, 910), "C", fill="black", font=_font(30))


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()
