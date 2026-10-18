"""Pure, database-free CSV parsing for the two-phase student roster import.

The parser only enforces file-level rules, per-field normalization, column
contract and within-batch student-code uniqueness. Every decision that needs
current database state (academic year bounds, class existence/status, name
matching, enrollment overlap) belongs to ``import_service`` so that preview and
confirmation share exactly one evaluator. Messages never echo student codes or
names; the frozen ``student_import_rows`` table keeps the normalized payload.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
MAX_PREVIEW_ENTRIES = 500
CODE_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")

MODES = ("student_only", "enrollment_only", "student_with_enrollment")
MODE_COLUMNS: dict[str, tuple[str, ...]] = {
    "student_only": ("student_code", "name"),
    "enrollment_only": ("student_code", "class_code", "valid_from"),
    "student_with_enrollment": ("student_code", "name", "class_code", "valid_from"),
}
ENROLLMENT_MODES = frozenset({"enrollment_only", "student_with_enrollment"})
NAME_MODES = frozenset({"student_only", "student_with_enrollment"})

# File-level error codes, mapped to a fixed HTTP status by the service layer.
FILE_EMPTY = "student_import_file_empty"
FILE_TOO_LARGE = "student_import_file_too_large"
ENCODING_UNSUPPORTED = "student_import_file_encoding_unsupported"
ENCODING_INVALID = "student_import_file_encoding_invalid"
CSV_MALFORMED = "student_import_csv_malformed"
COLUMNS_INVALID = "student_import_columns_invalid"
ROW_LIMIT_EXCEEDED = "student_import_row_limit_exceeded"

# Row-level codes. They are part of the frozen preview contract.
ROW_FIELD_COUNT_INVALID = "row_field_count_invalid"
STUDENT_CODE_REQUIRED = "student_code_required"
STUDENT_CODE_INVALID = "student_code_invalid"
DUPLICATE_STUDENT_CODE = "duplicate_student_code_in_batch"
STUDENT_CODE_CASE_AMBIGUOUS = "student_code_case_ambiguous"
NAME_REQUIRED = "name_required"
NAME_INVALID = "name_invalid"
CLASS_CODE_REQUIRED = "class_code_required"
CLASS_CODE_INVALID = "class_code_invalid"
VALID_FROM_REQUIRED = "valid_from_required"
VALID_FROM_INVALID = "valid_from_invalid"
DUPLICATE_NAME_EXISTING = "duplicate_name_existing"

_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_UTF32_BOMS = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")


class ImportFileError(Exception):
    """File-level rejection; ``code``/``status_code`` become an HTTP error."""

    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass
class RowIssue:
    """One structured, payload-free issue attached to a row."""

    field: str | None
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass
class ParsedRow:
    """Normalized CSV row before any database check."""

    row_number: int
    student_code: str | None = None
    normalized_student_code: str | None = None
    name: str | None = None
    class_code: str | None = None
    valid_from: date | None = None
    outcome: str | None = None
    errors: list[RowIssue] = field(default_factory=list)
    warnings: list[RowIssue] = field(default_factory=list)

    @property
    def row_state(self) -> str:
        if self.errors:
            return "error"
        if self.warnings:
            return "warning"
        return "valid"

    def add_error(self, field_name: str | None, code: str, message: str) -> None:
        self.errors.append(RowIssue(field_name, code, message))

    def add_warning(self, field_name: str | None, code: str, message: str) -> None:
        self.warnings.append(RowIssue(field_name, code, message))


@dataclass
class ParsedFile:
    """Successful parse result; database evaluation is still pending."""

    mode: str
    rows: list[ParsedRow]


def normalize_code(value: str) -> str:
    """Canonical code form: NFKC then surrounding whitespace stripped."""
    return unicodedata.normalize("NFKC", value).strip()


def normalize_name(value: str) -> str:
    """Canonical display/identity form: NFKC, trim, collapse inner whitespace."""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def code_is_valid(value: str) -> bool:
    return CODE_PATTERN.fullmatch(value) is not None


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def _unsupported_encoding(content: bytes) -> bool:
    """Explicitly reject UTF-16/32 (BOM) and binary input without guessing."""
    if content.startswith(_UTF32_BOMS) or content.startswith(_UTF16_BOMS):
        return True
    return b"\x00" in content


def _blank(cells: list[str]) -> bool:
    return all(not cell.strip() for cell in cells)


def _normalized_header(mode: str, cells: list[str]) -> list[str]:
    names = [unicodedata.normalize("NFKC", cell).strip().lower() for cell in cells]
    required = MODE_COLUMNS[mode]
    if len(set(names)) != len(names) or set(names) != set(required):
        raise ImportFileError(COLUMNS_INVALID, 422)
    return names


def _parse_row(header: list[str], cells: list[str], row_number: int) -> ParsedRow:
    row = ParsedRow(row_number=row_number)
    if len(cells) != len(header):
        row.add_error(None, ROW_FIELD_COUNT_INVALID, "列数与表头不一致")
        return row
    values = dict(zip(header, cells, strict=True))

    code = normalize_code(values.get("student_code", ""))
    if not code:
        row.add_error("student_code", STUDENT_CODE_REQUIRED, "学号不能为空")
    elif not code_is_valid(code):
        row.add_error("student_code", STUDENT_CODE_INVALID, "学号格式不正确")
    else:
        row.student_code = code
        row.normalized_student_code = code

    if "name" in values:
        name = normalize_name(values["name"])
        if not name:
            row.add_error("name", NAME_REQUIRED, "姓名不能为空")
        elif len(name) > 100 or _has_control_character(name):
            row.add_error("name", NAME_INVALID, "姓名格式不正确")
        else:
            row.name = name

    if "class_code" in values:
        class_code = normalize_code(values["class_code"])
        if not class_code:
            row.add_error("class_code", CLASS_CODE_REQUIRED, "班级编码不能为空")
        elif not code_is_valid(class_code):
            row.add_error("class_code", CLASS_CODE_INVALID, "班级编码格式不正确")
        else:
            row.class_code = class_code

    if "valid_from" in values:
        raw_date = values["valid_from"].strip()
        if not raw_date:
            row.add_error("valid_from", VALID_FROM_REQUIRED, "生效日期不能为空")
        else:
            try:
                row.valid_from = date.fromisoformat(raw_date)
            except ValueError:
                row.add_error("valid_from", VALID_FROM_INVALID, "生效日期格式不正确")

    return row


def _mark_batch_duplicates(rows: list[ParsedRow]) -> None:
    """Reject exact duplicates and case-only ambiguity; never guess identity."""
    by_code: dict[str, list[ParsedRow]] = {}
    for row in rows:
        if row.normalized_student_code is not None:
            by_code.setdefault(row.normalized_student_code, []).append(row)

    for group in by_code.values():
        if len(group) > 1:
            for row in group:
                row.add_error(
                    "student_code",
                    DUPLICATE_STUDENT_CODE,
                    "学号在批次内重复",
                )

    by_case: dict[str, set[str]] = {}
    for code in by_code:
        by_case.setdefault(code.casefold(), set()).add(code)
    ambiguous = {key for key, codes in by_case.items() if len(codes) > 1}
    for code, group in by_code.items():
        if code.casefold() in ambiguous:
            for row in group:
                row.add_error(
                    "student_code",
                    STUDENT_CODE_CASE_AMBIGUOUS,
                    "学号在批次内仅大小写不同，无法确认是否同一人",
                )


def parse_student_import_file(mode: str, content: bytes) -> ParsedFile:
    """Parse one roster CSV upload; raises ``ImportFileError`` on file errors."""
    if mode not in MODE_COLUMNS:
        raise ValueError(f"unsupported import mode: {mode}")
    if len(content) > MAX_FILE_BYTES:
        raise ImportFileError(FILE_TOO_LARGE, 413)
    if not content:
        raise ImportFileError(FILE_EMPTY, 422)
    if _unsupported_encoding(content):
        raise ImportFileError(ENCODING_UNSUPPORTED, 415)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportFileError(ENCODING_INVALID, 415) from None

    csv.field_size_limit(MAX_FILE_BYTES)
    reader = csv.reader(io.StringIO(text), strict=True)
    header: list[str] | None = None
    rows: list[ParsedRow] = []
    try:
        for cells in reader:
            row_number = reader.line_num
            if _blank(cells):
                continue
            if header is None:
                header = _normalized_header(mode, cells)
                continue
            rows.append(_parse_row(header, cells, row_number))
            if len(rows) > MAX_ROWS:
                raise ImportFileError(ROW_LIMIT_EXCEEDED, 413)
    except csv.Error:
        raise ImportFileError(CSV_MALFORMED, 422) from None

    if header is None or not rows:
        raise ImportFileError(FILE_EMPTY, 422)
    _mark_batch_duplicates(rows)
    return ParsedFile(mode=mode, rows=rows)


__all__ = [
    "CLASS_CODE_INVALID",
    "CLASS_CODE_REQUIRED",
    "COLUMNS_INVALID",
    "CODE_PATTERN",
    "CSV_MALFORMED",
    "DUPLICATE_NAME_EXISTING",
    "DUPLICATE_STUDENT_CODE",
    "ENCODING_INVALID",
    "ENCODING_UNSUPPORTED",
    "ENROLLMENT_MODES",
    "FILE_EMPTY",
    "FILE_TOO_LARGE",
    "ImportFileError",
    "MAX_FILE_BYTES",
    "MAX_PREVIEW_ENTRIES",
    "MAX_ROWS",
    "MODES",
    "MODE_COLUMNS",
    "NAME_INVALID",
    "NAME_MODES",
    "NAME_REQUIRED",
    "ParsedFile",
    "ParsedRow",
    "ROW_FIELD_COUNT_INVALID",
    "ROW_LIMIT_EXCEEDED",
    "RowIssue",
    "STUDENT_CODE_CASE_AMBIGUOUS",
    "STUDENT_CODE_INVALID",
    "STUDENT_CODE_REQUIRED",
    "VALID_FROM_INVALID",
    "VALID_FROM_REQUIRED",
    "code_is_valid",
    "normalize_code",
    "normalize_name",
    "parse_student_import_file",
]
