"""Pure unit contract for the roster-import CSV parser (no database, no network)."""

import pytest

from huipi_cloud.modules.organization import csv_import
from huipi_cloud.modules.organization.csv_import import (
    COLUMNS_INVALID,
    CSV_MALFORMED,
    ENCODING_INVALID,
    ENCODING_UNSUPPORTED,
    FILE_EMPTY,
    FILE_TOO_LARGE,
    MAX_FILE_BYTES,
    MAX_ROWS,
    ROW_LIMIT_EXCEEDED,
    ImportFileError,
    normalize_code,
    normalize_name,
    parse_student_import_file,
)


def parse(mode: str, text: str | bytes):
    content = text.encode() if isinstance(text, str) else text
    return parse_student_import_file(mode, content)


def codes(row) -> list[str]:
    return [issue.code for issue in row.errors]


def warnings(row) -> list[str]:
    return [issue.code for issue in row.warnings]


def expect_file_error(mode: str, content: str | bytes, code: str, status: int):
    with pytest.raises(ImportFileError) as error:
        parse(mode, content)
    assert error.value.code == code
    assert error.value.status_code == status
    assert str(error.value) == code


# ---------------------------------------------------------------------------
# Encoding and file-level limits
# ---------------------------------------------------------------------------


def test_utf8_and_utf8_bom_are_accepted_identically():
    body = "student_code,name\nS001,张三\n"
    plain = parse("student_only", body)
    bom = parse("student_only", b"\xef\xbb\xbf" + body.encode())
    assert [(row.student_code, row.name) for row in plain.rows] == [("S001", "张三")]
    assert [(row.student_code, row.name) for row in bom.rows] == [("S001", "张三")]


@pytest.mark.parametrize(
    "encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"]
)
def test_utf16_and_utf32_are_rejected_without_guessing(encoding):
    content = "student_code,name\nS001,张三\n".encode(encoding)
    expect_file_error("student_only", content, ENCODING_UNSUPPORTED, 415)


@pytest.mark.parametrize("content", [b"\x00\x01\x02\x03", b"%PDF-1.7\x00binary", b"\xff\xfe"])
def test_binary_content_is_rejected_as_unsupported_encoding(content):
    expect_file_error("student_only", content, ENCODING_UNSUPPORTED, 415)


def test_invalid_utf8_bytes_are_rejected_as_invalid_encoding():
    content = b"student_code,name\nS001,\xff\xfd\xfe\n"
    expect_file_error("student_only", content, ENCODING_INVALID, 415)


def test_empty_files_are_rejected():
    for content in (
        b"",
        b"\xef\xbb\xbf",
        b"student_code,name\n",
        b"student_code,name\n\n\n",
        b"student_code,name\n   \n\t\n",
    ):
        expect_file_error("student_only", content, FILE_EMPTY, 422)


def test_file_larger_than_two_mib_is_rejected_before_parsing():
    expect_file_error("student_only", b"x" * (MAX_FILE_BYTES + 1), FILE_TOO_LARGE, 413)


def test_more_than_max_rows_is_rejected():
    header = "student_code,name\n"
    body = "".join(f"S{index:05d},学生{index}\n" for index in range(MAX_ROWS + 1))
    expect_file_error("student_only", header + body, ROW_LIMIT_EXCEEDED, 413)


def test_exactly_max_rows_is_accepted():
    header = "student_code,name\n"
    body = "".join(f"S{index:05d},学生{index}\n" for index in range(MAX_ROWS))
    parsed = parse("student_only", header + body)
    assert len(parsed.rows) == MAX_ROWS


# ---------------------------------------------------------------------------
# Header and column contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,header",
    [
        ("student_only", "student_code,name"),
        ("enrollment_only", "student_code,class_code,valid_from"),
        ("student_with_enrollment", "student_code,name,class_code,valid_from"),
    ],
)
def test_required_header_per_mode_is_accepted(mode, header):
    cells = {
        "student_code": "S001",
        "name": "张三",
        "class_code": "G101",
        "valid_from": "2025-09-01",
    }
    row = ",".join(cells[column] for column in header.split(","))
    parsed = parse(mode, f"{header}\n{row}\n")
    assert len(parsed.rows) == 1
    assert parsed.rows[0].row_state == "valid"


def test_header_is_case_whitespace_and_nfkc_insensitive():
    body = " Student_Code ,　ＮＡＭＥ　\nS001,张三\n"
    parsed = parse("student_only", body)
    assert parsed.rows[0].student_code == "S001"
    assert parsed.rows[0].name == "张三"


def test_header_order_may_differ_from_documentation_order():
    parsed = parse("student_only", "name,student_code\n张三,S001\n")
    row = parsed.rows[0]
    assert (row.student_code, row.name, row.row_state) == ("S001", "张三", "valid")


@pytest.mark.parametrize(
    "mode,header",
    [
        ("student_only", "student_code"),
        ("student_only", "student_code,name,class_code"),
        ("enrollment_only", "student_code,name,class_code,valid_from"),
        ("student_with_enrollment", "student_code,name,class_code"),
        ("student_with_enrollment", "student_code,class_code,valid_from"),
        ("student_with_enrollment", "student_code,student_code,name,class_code"),
        ("student_with_enrollment", "student_code,name,class_code,valid_from,extra"),
        ("student_with_enrollment", "student_code,name,class_code,"),
    ],
)
def test_missing_duplicate_unknown_or_empty_column_is_rejected(mode, header):
    expect_file_error(mode, f"{header}\n", COLUMNS_INVALID, 422)


def test_csv_syntax_error_is_rejected_as_malformed():
    expect_file_error(
        "student_only",
        'student_code,name\n"S001,张三\n',
        CSV_MALFORMED,
        422,
    )
    expect_file_error(
        "student_only",
        'student_code,name\nS001,"张"三\n',
        CSV_MALFORMED,
        422,
    )


# ---------------------------------------------------------------------------
# Normalization and code pattern
# ---------------------------------------------------------------------------


def test_normalize_code_nfkc_and_strip():
    assert normalize_code("  Ｓ００１  ") == "S001"
    assert normalize_code("\u3000A.B_c-d\u3000") == "A.B_c-d"


def test_normalize_name_nfkc_strip_and_collapse_whitespace():
    assert normalize_name("  张  三  ") == "张 三"
    assert normalize_name("张\t三\n四") == "张 三 四"
    assert normalize_name("　李　四　") == "李 四"


@pytest.mark.parametrize(
    "code", ["A", "a", "0", "S001", "a.b_c-d", "A" * 64, "9-._"]
)
def test_code_pattern_accepts_documented_syntax(code):
    assert csv_import.code_is_valid(code) is True


@pytest.mark.parametrize(
    "code",
    ["", " S001", "S 001", "-S001", ".S001", "_S001", "S/001", "学号", "A" * 65],
)
def test_code_pattern_rejects_other_syntax(code):
    assert csv_import.code_is_valid(code) is False


def test_student_code_is_normalized_and_invalid_code_blocks_row():
    parsed = parse("student_only", "student_code,name\n Ｓ００１ ,张三\nS 002,李四\n")
    first, second = parsed.rows
    assert (first.student_code, first.normalized_student_code) == ("S001", "S001")
    assert first.row_state == "valid"
    assert codes(second) == ["student_code_invalid"]
    assert second.row_state == "error"


def test_empty_student_code_and_name_are_required_errors():
    parsed = parse("student_only", "student_code,name\n,张三\nS001,\n")
    assert codes(parsed.rows[0]) == ["student_code_required"]
    assert codes(parsed.rows[1]) == ["name_required"]


@pytest.mark.parametrize("name", ["张\x01三", "张\u200b三"])
def test_control_characters_in_name_are_rejected(name):
    parsed = parse("student_only", f"student_code,name\nS001,{name}\n")
    assert codes(parsed.rows[0]) == ["name_invalid"]


def test_name_longer_than_100_characters_is_rejected():
    parsed = parse("student_only", f"student_code,name\nS001,{'张' * 101}\n")
    assert codes(parsed.rows[0]) == ["name_invalid"]
    accepted = parse("student_only", f"student_code,name\nS001,{'张' * 100}\n")
    assert accepted.rows[0].row_state == "valid"


def test_class_code_required_and_validated_for_enrollment_modes():
    parsed = parse(
        "enrollment_only",
        "student_code,class_code,valid_from\nS001,,2025-09-01\nS002,G 101,2025-09-01\n",
    )
    assert codes(parsed.rows[0]) == ["class_code_required"]
    assert codes(parsed.rows[1]) == ["class_code_invalid"]


# ---------------------------------------------------------------------------
# valid_from
# ---------------------------------------------------------------------------


def test_valid_from_is_parsed_strictly():
    parsed = parse(
        "enrollment_only",
        "student_code,class_code,valid_from\n"
        "S001,G101,2025-09-01\n"
        "S002,G101,2025/09/01\n"
        "S003,G101,2025-13-01\n"
        "S004,G101,\n",
    )
    assert parsed.rows[0].valid_from.isoformat() == "2025-09-01"
    assert codes(parsed.rows[1]) == ["valid_from_invalid"]
    assert codes(parsed.rows[2]) == ["valid_from_invalid"]
    assert codes(parsed.rows[3]) == ["valid_from_required"]


# ---------------------------------------------------------------------------
# Physical line numbers and blank rows
# ---------------------------------------------------------------------------


def test_physical_line_numbers_survive_blank_and_quoted_multiline_rows():
    content = (
        "student_code,name\n"
        "\n"
        "S001,张三\n"
        "\n"
        'S002,"李\n四"\n'
        "\n"
        "S003,王五\n"
    )
    parsed = parse("student_only", content)
    assert [row.row_number for row in parsed.rows] == [3, 6, 8]
    assert [(row.student_code, row.name) for row in parsed.rows] == [
        ("S001", "张三"),
        ("S002", "李 四"),
        ("S003", "王五"),
    ]


def test_blank_lines_are_skipped_without_becoming_rows():
    parsed = parse("student_only", "\n\nstudent_code,name\n\n\nS001,张三\n\n")
    assert len(parsed.rows) == 1
    assert parsed.rows[0].row_number == 6


def test_row_with_wrong_field_count_is_a_row_error_not_a_file_error():
    parsed = parse("student_only", "student_code,name\nS001\nS002,李四,多余\n")
    for row in parsed.rows:
        assert codes(row) == ["row_field_count_invalid"]
        assert row.row_state == "error"
        assert row.student_code is None and row.name is None


# ---------------------------------------------------------------------------
# Within-batch student-code uniqueness
# ---------------------------------------------------------------------------


def test_identical_student_code_repeats_are_rejected_for_every_matching_row():
    parsed = parse("student_only", "student_code,name\nS001,张三\nS002,李四\nS001,王五\n")
    assert codes(parsed.rows[0]) == ["duplicate_student_code_in_batch"]
    assert codes(parsed.rows[1]) == []
    assert codes(parsed.rows[2]) == ["duplicate_student_code_in_batch"]


def test_case_only_student_code_difference_is_ambiguous_and_never_guessed():
    parsed = parse("student_only", "student_code,name\nS001,张三\ns001,李四\n")
    assert codes(parsed.rows[0]) == ["student_code_case_ambiguous"]
    assert codes(parsed.rows[1]) == ["student_code_case_ambiguous"]
    assert parsed.rows[0].row_state == "error"


def test_exact_and_case_ambiguous_codes_can_both_be_reported():
    parsed = parse("student_only", "student_code,name\nS001,张三\nS001,李四\ns001,王五\n")
    assert codes(parsed.rows[0]) == [
        "duplicate_student_code_in_batch",
        "student_code_case_ambiguous",
    ]
    assert codes(parsed.rows[1]) == [
        "duplicate_student_code_in_batch",
        "student_code_case_ambiguous",
    ]
    assert codes(parsed.rows[2]) == ["student_code_case_ambiguous"]


def test_rows_with_parse_errors_do_not_participate_in_duplicate_detection():
    parsed = parse("student_only", "student_code,name\nS 001,张三\nS 001,李四\n")
    for row in parsed.rows:
        assert codes(row) == ["student_code_invalid"]


def test_issue_entries_expose_only_field_code_message():
    parsed = parse("student_only", "student_code,name\nS001\n")
    issue = parsed.rows[0].errors[0]
    assert issue.as_dict() == {
        "field": None,
        "code": "row_field_count_invalid",
        "message": "列数与表头不一致",
    }


def test_parsed_rows_expose_state_and_no_warnings_by_default():
    parsed = parse("student_only", "student_code,name\nS001,张三\n")
    row = parsed.rows[0]
    assert row.row_state == "valid"
    assert row.outcome is None
    assert row.warnings == []
    row.add_warning("name", "synthetic_warning", "测试")
    assert row.row_state == "warning"
