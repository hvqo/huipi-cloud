"""Build the checked-in, visibly synthetic visual-evidence dataset."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.visual_samples import create_visual_samples

ROOT = Path(__file__).resolve().parents[1]
DATASET_ROOT = ROOT / "datasets" / "visual-evidence" / "synthetic-v1"
WIDTH = 1000
HEIGHT = 1400
INK = "#174fc4"
FONT_PATH = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def _font(size: int) -> ImageFont.ImageFont:
    if FONT_PATH.is_file():
        return ImageFont.truetype(str(FONT_PATH), size=size)
    return ImageFont.load_default()


def _normalized(box: tuple[int, int, int, int]) -> list[float]:
    x0, y0, x1, y1 = box
    return [x0 / WIDTH, y0 / HEIGHT, x1 / WIDTH, y1 / HEIGHT]


def _new_page(
    question_number: int | None = 1,
    stem: str = "Solve 2 + 2. Show your work.",
    *,
    question_y: int = 150,
    answer_box: tuple[int, int, int, int] = (80, 300, 920, 1120),
) -> tuple[Image.Image, ImageDraw.ImageDraw, tuple[int, int, int, int]]:
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw.text(
        (35, 25),
        "SYNTHETIC - GENERATED TEST PAGE - NOT STUDENT WORK",
        fill="#666666",
        font=_font(19),
    )
    prompt = ""
    if question_number is not None:
        prompt = f"Question {question_number}: {stem}"
        draw.text((80, question_y), prompt, fill="#111111", font=_font(34))
    draw.rectangle(answer_box, outline="#777777", width=3)
    prompt_box = draw.textbbox((80, question_y), prompt, font=_font(34)) if prompt else (0, 0, 0, 0)
    return image, draw, prompt_box


def _region(
    identifier: str,
    page_index: int,
    box: tuple[int, int, int, int],
    label: str,
    role: str,
    question_number: int | None,
) -> dict[str, object]:
    return {
        "region_id": identifier,
        "page_index": page_index,
        "bbox": _normalized(box),
        "label": label,
        "role": role,
        "question_number": question_number,
    }


def _stroke_four(draw: ImageDraw.ImageDraw, left: int, top: int, color: str = INK) -> None:
    draw.line(
        [(left + 75, top), (left + 20, top + 75), (left + 95, top + 73)],
        fill=color,
        width=8,
    )
    draw.line([(left + 78, top - 4), (left + 76, top + 105)], fill=color, width=8)


def _scenario(name: str) -> tuple[list[Image.Image], dict[str, object]]:
    stem = "Solve 2 + 2. Show your work."
    images: list[Image.Image] = []
    regions: list[dict[str, object]] = []
    page, draw, prompt_box = _new_page(stem=stem)
    images.append(page)
    regions.append(_region("target-prompt", 0, prompt_box, "printed_prompt", "prompt", 1))
    presence = "response_absent"
    attribution: dict[str, object] = {"status": "not_applicable", "question_number": None}

    if name == "baseline_print_only":
        text = "Printed instructions only."
        draw.text((145, 520), text, fill="#222222", font=_font(34))
        box = draw.textbbox((145, 520), text, font=_font(34))
        regions.append(_region("printed-answer-area-text", 0, box, "printed_prompt", "prompt", 1))
    elif name == "baseline_mixed_ink":
        _stroke_four(draw, 345, 505)
        regions.append(
            _region(
                "simulated-blue-four", 0, (360, 500, 445, 620), "handwritten_text", "response", 1
            )
        )
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name == "baseline_formula_ink":
        draw.line([(220, 555), (270, 625), (325, 555)], fill=INK, width=8)
        draw.line([(390, 590), (475, 590)], fill=INK, width=7)
        draw.line([(432, 552), (432, 626)], fill=INK, width=7)
        draw.text((515, 555), "= 4", fill=INK, font=_font(64))
        regions.append(
            _region(
                "simulated-formula", 0, (215, 545, 660, 635), "handwritten_formula", "response", 1
            )
        )
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name == "baseline_geometry_mark":
        draw.line([(250, 900), (500, 570), (760, 900), (250, 900)], fill="#222222", width=6)
        draw.line([(500, 570), (500, 900)], fill=INK, width=7)
        draw.ellipse((488, 705, 512, 729), outline=INK, width=5)
        draw.line([(475, 890), (500, 890), (500, 915)], fill=INK, width=5)
        draw.text((220, 910), "A", fill="black", font=_font(30))
        draw.text((490, 525), "B", fill="black", font=_font(30))
        draw.text((760, 910), "C", fill="black", font=_font(30))
        regions.append(
            _region("blue-geometry-marks", 0, (465, 560, 520, 920), "geometry_mark", "response", 1)
        )
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name == "baseline_blank_with_print":
        text = "No marks in the answer area."
        draw.text((145, 520), text, fill="#444444", font=_font(34))
        regions.append(
            _region(
                "printed-no-marks-message",
                0,
                draw.textbbox((145, 520), text, font=_font(34)),
                "printed_prompt",
                "prompt",
                1,
            )
        )
    elif name == "blank_answer_area":
        pass
    elif name == "dense_print_only":
        lines = [
            "Example: first read the question.",
            "Then show a method in this box.",
            "No student response is shown.",
        ]
        for offset, text in enumerate(lines):
            y = 430 + offset * 62
            draw.text((140, y), text, fill="#222222", font=_font(29))
            box = draw.textbbox((140, y), text, font=_font(29))
            regions.append(
                _region(f"printed-line-{offset + 1}", 0, box, "printed_prompt", "prompt", 1)
            )
    elif name == "simulated_sparse_four":
        _stroke_four(draw, 445, 650)
        regions.append(
            _region(
                "sparse-simulated-four", 0, (460, 645, 545, 770), "handwritten_text", "response", 1
            )
        )
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name == "unclear_single_dash":
        draw.line([(470, 680), (545, 665)], fill="#777777", width=4)
        regions.append(
            _region("ambiguous-dash", 0, (465, 655, 550, 690), "unclear", "response", None)
        )
        presence, attribution = "uncertain", {"status": "uncertain", "question_number": None}
    elif name == "simulated_fraction_formula":
        draw.text((260, 500), "1", fill=INK, font=_font(58))
        draw.line([(250, 570), (310, 570)], fill=INK, width=6)
        draw.text((260, 575), "2", fill=INK, font=_font(58))
        draw.text((350, 540), "+", fill=INK, font=_font(58))
        draw.text((450, 500), "1", fill=INK, font=_font(58))
        draw.line([(440, 570), (500, 570)], fill=INK, width=6)
        draw.text((450, 575), "2", fill=INK, font=_font(58))
        draw.text((545, 540), "= 1", fill=INK, font=_font(58))
        regions.append(
            _region(
                "fraction-formula", 0, (245, 495, 660, 655), "handwritten_formula", "response", 1
            )
        )
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name in {"table_with_fill", "blank_table"}:
        left, top, right, bottom = 220, 480, 780, 830
        draw.rectangle((left, top, right, bottom), outline="#222222", width=3)
        for x in (left + 185, left + 370):
            draw.line([(x, top), (x, bottom)], fill="#222222", width=3)
        draw.line([(left, top + 115), (right, top + 115)], fill="#222222", width=3)
        draw.line([(left, top + 230), (right, top + 230)], fill="#222222", width=3)
        draw.text((left + 25, top + 35), "x", fill="#111111", font=_font(38))
        draw.text((left + 210, top + 35), "2", fill="#111111", font=_font(38))
        draw.text((left + 395, top + 35), "4", fill="#111111", font=_font(38))
        if name == "table_with_fill":
            draw.text((left + 240, top + 145), "4", fill=INK, font=_font(58))
            regions.append(
                _region(
                    "filled-table-cell",
                    0,
                    (left + 205, top + 120, left + 365, top + 220),
                    "student_filled_table",
                    "response",
                    1,
                )
            )
            presence, attribution = (
                "response_present",
                {"status": "target_question", "question_number": 1},
            )
        else:
            regions.append(
                _region(
                    "printed-table", 0, (left, top, right, bottom), "printed_prompt", "prompt", 1
                )
            )
    elif name == "two_questions_target_one":
        page = Image.new("RGB", (WIDTH, HEIGHT), "white")
        draw = ImageDraw.Draw(page)
        draw.text(
            (35, 25),
            "SYNTHETIC - GENERATED TEST PAGE - NOT STUDENT WORK",
            fill="#666666",
            font=_font(19),
        )
        q1 = "Question 1: Solve 2 + 2."
        q2 = "Question 2: Solve 3 + 3."
        draw.text((80, 130), q1, fill="#111111", font=_font(34))
        draw.rectangle((80, 235, 920, 675), outline="#777777", width=3)
        draw.text((80, 735), q2, fill="#111111", font=_font(34))
        draw.rectangle((80, 840, 920, 1280), outline="#777777", width=3)
        q1box = draw.textbbox((80, 130), q1, font=_font(34))
        q2box = draw.textbbox((80, 735), q2, font=_font(34))
        regions = [
            _region("target-prompt", 0, q1box, "printed_prompt", "prompt", 1),
            _region("other-prompt", 0, q2box, "printed_prompt", "prompt", 2),
        ]
        _stroke_four(draw, 390, 380)
        regions.append(
            _region("q1-answer", 0, (405, 375, 490, 500), "handwritten_text", "response", 1)
        )
        images = [page]
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name == "other_question_answer_only":
        page = Image.new("RGB", (WIDTH, HEIGHT), "white")
        draw = ImageDraw.Draw(page)
        draw.text(
            (35, 25),
            "SYNTHETIC - GENERATED TEST PAGE - NOT STUDENT WORK",
            fill="#666666",
            font=_font(19),
        )
        q1 = "Question 1: Solve 2 + 2."
        q2 = "Question 2: Solve 3 + 3."
        draw.text((80, 130), q1, fill="#111111", font=_font(34))
        draw.rectangle((80, 235, 920, 675), outline="#777777", width=3)
        draw.text((80, 735), q2, fill="#111111", font=_font(34))
        draw.rectangle((80, 840, 920, 1280), outline="#777777", width=3)
        q1box = draw.textbbox((80, 130), q1, font=_font(34))
        q2box = draw.textbbox((80, 735), q2, font=_font(34))
        regions = [
            _region("target-prompt", 0, q1box, "printed_prompt", "prompt", 1),
            _region("other-prompt", 0, q2box, "printed_prompt", "prompt", 2),
        ]
        _stroke_four(draw, 390, 990)
        regions.append(
            _region("q2-answer", 0, (405, 985, 490, 1110), "handwritten_text", "response", 2)
        )
        images = [page]
        presence, attribution = (
            "response_absent",
            {"status": "other_question", "question_number": 2},
        )
    elif name == "shared_geometry_uncertain":
        page = Image.new("RGB", (WIDTH, HEIGHT), "white")
        draw = ImageDraw.Draw(page)
        draw.text(
            (35, 25),
            "SYNTHETIC - GENERATED TEST PAGE - NOT STUDENT WORK",
            fill="#666666",
            font=_font(19),
        )
        q1 = "Question 1: Find angle A."
        q2 = "Question 2: Find angle B."
        draw.text((80, 130), q1, fill="#111111", font=_font(34))
        draw.text((80, 220), q2, fill="#111111", font=_font(34))
        draw.rectangle((80, 315, 920, 1190), outline="#777777", width=3)
        draw.line([(250, 960), (500, 450), (770, 960), (250, 960)], fill="#222222", width=6)
        draw.line([(500, 450), (500, 960)], fill=INK, width=7)
        regions = [
            _region(
                "target-prompt",
                0,
                draw.textbbox((80, 130), q1, font=_font(34)),
                "printed_prompt",
                "prompt",
                1,
            ),
            _region(
                "second-prompt",
                0,
                draw.textbbox((80, 220), q2, font=_font(34)),
                "printed_prompt",
                "prompt",
                2,
            ),
            _region("shared-mark", 0, (490, 440, 510, 965), "geometry_mark", "response", None),
        ]
        images = [page]
        presence, attribution = "uncertain", {"status": "uncertain", "question_number": None}
    elif name == "blurred_response_uncertain":
        _stroke_four(draw, 390, 560, color="#aab6ca")
        images[0] = page.filter(ImageFilter.GaussianBlur(radius=7))
        regions.append(_region("blurred-ink", 0, (405, 555, 490, 680), "unclear", "response", None))
        presence, attribution = "uncertain", {"status": "uncertain", "question_number": None}
    elif name == "answer_near_prompt":
        _stroke_four(draw, 760, 235)
        regions.append(
            _region("ink-near-prompt", 0, (775, 230, 860, 355), "handwritten_text", "response", 1)
        )
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name == "cross_page_response":
        continuation, continuation_draw, continuation_prompt = _new_page(
            stem="Continue the solution here.",
            question_y=160,
            answer_box=(80, 300, 920, 1120),
        )
        continuation_draw.text((370, 520), "continued", fill="#777777", font=_font(24))
        _stroke_four(continuation_draw, 480, 675)
        images.append(continuation)
        regions.append(
            _region("continuation-prompt", 1, continuation_prompt, "printed_prompt", "prompt", 1)
        )
        regions.append(
            _region("page-two-answer", 1, (495, 670, 580, 795), "handwritten_text", "response", 1)
        )
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name == "visible_geometry_no_text":
        draw.line([(300, 940), (500, 580), (700, 940)], fill="#222222", width=6)
        draw.line([(475, 820), (525, 820)], fill=INK, width=7)
        draw.ellipse((480, 800, 520, 840), outline=INK, width=5)
        regions.append(
            _region("geometry-only-mark", 0, (470, 790, 530, 850), "geometry_mark", "response", 1)
        )
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name == "printed_solution_distractor":
        text = "Worked example: 2 + 2 = 4"
        draw.text((180, 600), text, fill="#111111", font=_font(37))
        regions.append(
            _region(
                "printed-worked-example",
                0,
                draw.textbbox((180, 600), text, font=_font(37)),
                "printed_prompt",
                "prompt",
                1,
            )
        )
    elif name == "crossed_out_simulated_answer":
        _stroke_four(draw, 360, 560)
        draw.line([(330, 610), (510, 610)], fill="#bd2b2b", width=5)
        regions.append(
            _region(
                "crossed-out-answer", 0, (330, 550, 515, 675), "handwritten_text", "response", 1
            )
        )
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name == "low_contrast_formula":
        draw.line([(310, 610), (365, 670), (420, 610)], fill="#9ba9bf", width=6)
        draw.line([(475, 640), (545, 640)], fill="#9ba9bf", width=5)
        draw.text((565, 605), "= 4", fill="#9ba9bf", font=_font(42))
        regions.append(
            _region(
                "low-contrast-formula",
                0,
                (300, 600, 640, 690),
                "handwritten_formula",
                "response",
                1,
            )
        )
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name == "ambiguous_shared_step":
        draw.text((450, 570), "4", fill=INK, font=_font(58))
        regions.append(
            _region("ambiguous-shared-answer", 0, (445, 565, 495, 635), "unclear", "response", None)
        )
        presence, attribution = "uncertain", {"status": "uncertain", "question_number": None}
    elif name == "formula_equals_only":
        draw.line([(380, 660), (485, 660)], fill=INK, width=8)
        draw.line([(380, 690), (485, 690)], fill=INK, width=8)
        draw.text((520, 630), "4", fill=INK, font=_font(52))
        regions.append(
            _region("sparse-formula", 0, (370, 620, 585, 710), "handwritten_formula", "response", 1)
        )
        presence, attribution = (
            "response_present",
            {"status": "target_question", "question_number": 1},
        )
    elif name == "two_questions_target_two":
        page = Image.new("RGB", (WIDTH, HEIGHT), "white")
        draw = ImageDraw.Draw(page)
        draw.text(
            (35, 25),
            "SYNTHETIC - GENERATED TEST PAGE - NOT STUDENT WORK",
            fill="#666666",
            font=_font(19),
        )
        q1 = "Question 1: Solve 2 + 2."
        q2 = "Question 2: Solve 3 + 3."
        draw.text((80, 130), q1, fill="#111111", font=_font(34))
        draw.rectangle((80, 235, 920, 675), outline="#777777", width=3)
        draw.text((80, 735), q2, fill="#111111", font=_font(34))
        draw.rectangle((80, 840, 920, 1280), outline="#777777", width=3)
        q1box = draw.textbbox((80, 130), q1, font=_font(34))
        q2box = draw.textbbox((80, 735), q2, font=_font(34))
        _stroke_four(draw, 390, 990)
        images = [page]
        regions = [
            _region("target-prompt", 0, q2box, "printed_prompt", "prompt", 2),
            _region("other-prompt", 0, q1box, "printed_prompt", "prompt", 1),
            _region("q2-answer", 0, (405, 985, 490, 1110), "handwritten_text", "response", 2),
        ]
        return images, _make_case(
            name,
            2,
            "Solve 3 + 3. Show your work.",
            "response_present",
            {"status": "target_question", "question_number": 2},
            regions,
        )
    elif name == "tiny_mark_uncertain":
        draw.point((505, 700), fill=INK)
        draw.point((510, 704), fill=INK)
        regions.append(
            _region("tiny-ambiguous-mark", 0, (500, 695, 515, 710), "unclear", "response", None)
        )
        presence, attribution = "uncertain", {"status": "uncertain", "question_number": None}
    else:
        raise ValueError(f"unknown case: {name}")

    return images, _make_case(name, 1, stem, presence, attribution, regions)


def _make_case(
    name: str,
    question_number: int,
    stem: str,
    presence: str,
    attribution: dict[str, object],
    regions: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "case_id": name,
        "source_kind": "programmatic_raster",
        "dataset_kind": "synthetic",
        "question_number": question_number,
        "question_stem": stem,
        "manual_answer_presence": presence,
        "question_attribution": attribution,
        "ground_truth_regions": regions,
        "annotation_provenance": (
            "Region boxes and labels were hand-authored from this deterministic drawing recipe. "
            "All simulated ink is programmatically drawn; no real student image or "
            "human handwriting is used."
        ),
    }


def _save_case(
    case: dict[str, object],
    images: list[Image.Image],
    dataset_root: Path,
) -> dict[str, object]:
    case_root = dataset_root / "images" / str(case["case_id"])
    case_root.mkdir(parents=True, exist_ok=True)
    pages = []
    for page_index, image in enumerate(images):
        image_path = case_root / f"page-{page_index}.png"
        image.save(image_path, format="PNG", optimize=True)
        page_bytes = image_path.read_bytes()
        pages.append(
            {
                "page_index": page_index,
                "image_path": image_path.relative_to(dataset_root).as_posix(),
                "content_type": "image/png",
                "width": image.width,
                "height": image.height,
                "sha256": hashlib.sha256(page_bytes).hexdigest(),
            }
        )
    case["pages"] = pages
    return case


def build_dataset(dataset_root: Path = DATASET_ROOT) -> Path:
    dataset_root.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, object]] = []
    names = [
        "baseline_print_only",
        "baseline_mixed_ink",
        "baseline_formula_ink",
        "baseline_geometry_mark",
        "baseline_blank_with_print",
        "blank_answer_area",
        "dense_print_only",
        "simulated_sparse_four",
        "unclear_single_dash",
        "simulated_fraction_formula",
        "table_with_fill",
        "blank_table",
        "two_questions_target_one",
        "other_question_answer_only",
        "shared_geometry_uncertain",
        "blurred_response_uncertain",
        "answer_near_prompt",
        "cross_page_response",
        "visible_geometry_no_text",
        "printed_solution_distractor",
        "crossed_out_simulated_answer",
        "low_contrast_formula",
        "ambiguous_shared_step",
        "formula_equals_only",
        "two_questions_target_two",
        "tiny_mark_uncertain",
    ]
    legacy = create_visual_samples(dataset_root / ".legacy-seed")
    for name in names:
        if name in {
            "baseline_print_only",
            "baseline_mixed_ink",
            "baseline_formula_ink",
            "baseline_geometry_mark",
            "baseline_blank_with_print",
        }:
            original_name = {
                "baseline_print_only": "print_only",
                "baseline_mixed_ink": "mixed_handwriting",
                "baseline_formula_ink": "formula",
                "baseline_geometry_mark": "geometry",
                "baseline_blank_with_print": "blank_region",
            }[name]
            image = Image.open(legacy[original_name]).convert("RGB")
            stem = "Solve 2 + 2. Show your work."
            legacy_question = "Question 1: Solve 2 + 2. Show your work."
            prompt_box = ImageDraw.Draw(image).textbbox((90, 180), legacy_question, font=_font(42))
            regions = [_region("target-prompt", 0, prompt_box, "printed_prompt", "prompt", 1)]
            if name == "baseline_print_only":
                box = ImageDraw.Draw(image).textbbox(
                    (150, 500), "Printed prompt only.", font=_font(34)
                )
                regions.append(
                    _region("printed-answer-area-text", 0, box, "printed_prompt", "prompt", 1)
                )
                presence, attribution = (
                    "response_absent",
                    {"status": "not_applicable", "question_number": None},
                )
            elif name == "baseline_blank_with_print":
                box = ImageDraw.Draw(image).textbbox(
                    (150, 500), "No marks in the answer area.", font=_font(34)
                )
                regions.append(
                    _region("printed-no-marks-message", 0, box, "printed_prompt", "prompt", 1)
                )
                presence, attribution = (
                    "response_absent",
                    {"status": "not_applicable", "question_number": None},
                )
            elif name == "baseline_mixed_ink":
                regions.append(
                    _region(
                        "simulated-blue-four",
                        0,
                        (285, 552, 400, 715),
                        "handwritten_text",
                        "response",
                        1,
                    )
                )
                presence, attribution = (
                    "response_present",
                    {"status": "target_question", "question_number": 1},
                )
            elif name == "baseline_formula_ink":
                regions.append(
                    _region(
                        "simulated-formula",
                        0,
                        (180, 575, 650, 655),
                        "handwritten_formula",
                        "response",
                        1,
                    )
                )
                presence, attribution = (
                    "response_present",
                    {"status": "target_question", "question_number": 1},
                )
            else:
                regions.append(
                    _region(
                        "printed-geometry-diagram",
                        0,
                        (220, 525, 780, 915),
                        "printed_prompt",
                        "prompt",
                        1,
                    )
                )
                regions.append(
                    _region(
                        "blue-geometry-marks",
                        0,
                        (475, 570, 512, 915),
                        "geometry_mark",
                        "response",
                        1,
                    )
                )
                presence, attribution = (
                    "response_present",
                    {"status": "target_question", "question_number": 1},
                )
            case = _make_case(name, 1, stem, presence, attribution, regions)
            cases.append(_save_case(case, [image], dataset_root))
            image.close()
            continue
        images, case = _scenario(name)
        cases.append(_save_case(case, images, dataset_root))

    shutil.rmtree(dataset_root / ".legacy-seed", ignore_errors=True)
    dataset = {
        "schema_name": "huipi.visual.evidence.dataset",
        "schema_version": "1.0",
        "dataset_id": "synthetic_visual_evidence_baseline",
        "dataset_version": "1.0.0",
        "dataset_kind": "synthetic",
        "cases": cases,
    }
    output = dataset_root / "dataset.json"
    output.write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


if __name__ == "__main__":
    print(build_dataset())
