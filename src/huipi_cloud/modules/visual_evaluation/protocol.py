"""Strict, versioned annotations for controlled visual-evidence evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import warnings
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator

VISUAL_EVALUATION_DATASET_NAME = "huipi.visual.evidence.dataset"
VISUAL_EVALUATION_DATASET_VERSION = "1.0"

AnswerPresence = Literal["response_present", "response_absent", "uncertain"]
RegionType = Literal[
    "printed_prompt",
    "handwritten_text",
    "handwritten_formula",
    "student_filled_table",
    "geometry_mark",
    "unclear",
    "mixed_print_and_handwriting",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EvaluationPage(StrictModel):
    """A page image stored as a relative, digest-pinned dataset asset."""

    page_index: int = Field(ge=0)
    image_path: str = Field(min_length=1, max_length=512)
    content_type: Literal["image/png", "image/jpeg"]
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        path = PurePosixPath(self.image_path)
        if (
            path.is_absolute()
            or "\\" in self.image_path
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("image_path must be a normalized relative POSIX path")
        return self


class GroundTruthRegion(StrictModel):
    """A manually specified visual region, independent of any model output."""

    region_id: str = Field(min_length=1, max_length=64)
    page_index: int = Field(ge=0)
    bbox: tuple[float, float, float, float]
    label: RegionType
    role: Literal["prompt", "response"]
    question_number: int | None = Field(default=None, gt=0)

    @model_validator(mode="before")
    @classmethod
    def normalize_bbox(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        bbox = value.get("bbox")
        if isinstance(bbox, (tuple, list)):
            if len(bbox) != 4 or any(
                isinstance(point, bool)
                or not isinstance(point, (int, float))
                or not math.isfinite(float(point))
                for point in bbox
            ):
                raise ValueError("bbox must contain four finite numeric coordinates")
            return {**value, "bbox": tuple(float(point) for point in bbox)}
        return value

    @model_validator(mode="after")
    def validate_bbox_and_role(self) -> Self:
        x0, y0, x1, y1 = self.bbox
        if min(x0, y0, x1, y1) < 0 or max(x0, y0, x1, y1) > 1:
            raise ValueError("bbox coordinates must be normalized to [0, 1]")
        if x1 <= x0 or y1 <= y0:
            raise ValueError("bbox must be non-empty")
        if self.role == "prompt" and self.label != "printed_prompt":
            raise ValueError("prompt regions must use the printed_prompt label")
        if self.role == "response" and self.label == "printed_prompt":
            raise ValueError("response regions cannot use the printed_prompt label")
        return self


class QuestionAttribution(StrictModel):
    status: Literal["target_question", "other_question", "uncertain", "not_applicable"]
    question_number: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_question_number(self) -> Self:
        if self.status in {"target_question", "other_question"}:
            if self.question_number is None:
                raise ValueError("attributed content requires a question_number")
        elif self.question_number is not None:
            raise ValueError("uncertain or not-applicable attribution cannot set question_number")
        return self


class VisualEvaluationCase(StrictModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    source_kind: Literal[
        "programmatic_raster",
        "programmatic_pdf",
        "mineru_synthetic",
        "authorized_real",
    ]
    dataset_kind: Literal["synthetic", "authorized_real"]
    question_number: int = Field(gt=0)
    question_stem: str = Field(min_length=1, max_length=3000)
    pages: list[EvaluationPage] = Field(min_length=1, max_length=20)
    manual_answer_presence: AnswerPresence
    question_attribution: QuestionAttribution
    ground_truth_regions: list[GroundTruthRegion] = Field(max_length=100)
    annotation_provenance: str = Field(min_length=1, max_length=1000)
    authorization_ref: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_case_consistency(self) -> Self:
        page_indexes = [page.page_index for page in self.pages]
        if len(page_indexes) != len(set(page_indexes)):
            raise ValueError("page indexes must be unique within a case")
        available_pages = set(page_indexes)
        region_ids = [region.region_id for region in self.ground_truth_regions]
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("region_id values must be unique within a case")
        region_signatures = [
            (
                region.page_index,
                region.bbox,
                region.label,
                region.role,
                region.question_number,
            )
            for region in self.ground_truth_regions
        ]
        if len(region_signatures) != len(set(region_signatures)):
            raise ValueError("duplicate ground-truth regions are not allowed")
        target_prompts = []
        for region in self.ground_truth_regions:
            if region.page_index not in available_pages:
                raise ValueError("ground-truth region references a missing page")
            if region.role == "prompt" and region.question_number == self.question_number:
                target_prompts.append(region)
        if not target_prompts:
            raise ValueError("each case requires a prompt region for its target question")

        target_responses = [
            region
            for region in self.ground_truth_regions
            if region.role == "response" and region.question_number == self.question_number
        ]
        if self.manual_answer_presence == "response_present":
            if not target_responses:
                raise ValueError("response_present requires a target-question response region")
            if (
                self.question_attribution.status != "target_question"
                or self.question_attribution.question_number != self.question_number
            ):
                raise ValueError("response_present requires target-question attribution")
        elif self.manual_answer_presence == "response_absent":
            if target_responses:
                raise ValueError("response_absent cannot contain a target response region")
            if self.question_attribution.status not in {"other_question", "not_applicable"}:
                raise ValueError(
                    "response_absent requires other_question or not_applicable attribution"
                )
        elif self.question_attribution.status != "uncertain":
            raise ValueError("uncertain presence requires uncertain question attribution")

        if self.question_attribution.status == "target_question":
            if self.question_attribution.question_number != self.question_number:
                raise ValueError("target attribution must match the case question_number")
        elif self.question_attribution.status == "other_question":
            if self.question_attribution.question_number == self.question_number:
                raise ValueError("other-question attribution cannot match the target question")

        if self.dataset_kind == "authorized_real" and not self.authorization_ref:
            raise ValueError("authorized_real cases require an authorization reference")
        if self.dataset_kind == "synthetic" and self.authorization_ref is not None:
            raise ValueError("synthetic cases cannot contain an authorization reference")
        if self.source_kind == "authorized_real" and self.dataset_kind != "authorized_real":
            raise ValueError("authorized_real source_kind requires authorized_real dataset_kind")
        if self.dataset_kind == "authorized_real" and self.source_kind != "authorized_real":
            raise ValueError("authorized_real dataset_kind requires authorized_real source_kind")
        return self


class VisualEvaluationDataset(StrictModel):
    schema_name: Literal["huipi.visual.evidence.dataset"] = VISUAL_EVALUATION_DATASET_NAME
    schema_version: Literal["1.0"] = VISUAL_EVALUATION_DATASET_VERSION
    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,99}$")
    dataset_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    dataset_kind: Literal["synthetic", "authorized_real"]
    cases: list[VisualEvaluationCase] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_dataset(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case_id values must be unique in a dataset")
        if any(case.dataset_kind != self.dataset_kind for case in self.cases):
            raise ValueError("dataset_kind must be consistent across the dataset")
        return self


def load_dataset(path: Path, *, max_page_bytes: int = 20 * 1024 * 1024) -> VisualEvaluationDataset:
    """Load the dataset and verify every referenced file, size, format, dimensions, and digest."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    dataset = VisualEvaluationDataset.model_validate(payload)
    root = path.resolve().parent
    for case in dataset.cases:
        for page in case.pages:
            image_path = (root / page.image_path).resolve()
            if root not in image_path.parents or not image_path.is_file():
                raise ValueError(f"{case.case_id}: image file is missing or escapes dataset root")
            if image_path.stat().st_size > max_page_bytes:
                raise ValueError(f"{case.case_id}: image exceeds the configured byte limit")
            digest = _sha256_file(image_path)
            if digest != page.sha256:
                raise ValueError(f"{case.case_id}: image SHA-256 does not match the dataset")
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(image_path) as image:
                        actual_format = image.format
                        actual_size = image.size
                        image.verify()
                with Image.open(image_path) as image:
                    image.load()
            except (
                OSError,
                UnidentifiedImageError,
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
            ) as error:
                raise ValueError(f"{case.case_id}: image cannot be safely decoded") from error
            expected_format = "PNG" if page.content_type == "image/png" else "JPEG"
            if actual_format != expected_format:
                raise ValueError(f"{case.case_id}: image format does not match content_type")
            if actual_size != (page.width, page.height):
                raise ValueError(f"{case.case_id}: image dimensions changed")
            if page.width * page.height > 50_000_000:
                raise ValueError(f"{case.case_id}: image exceeds the pixel limit")
    return dataset


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
