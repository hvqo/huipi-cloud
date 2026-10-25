"""No network: decimal projection contracts and non-executable OOXML cells."""

from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from uuid import uuid4
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from huipi_cloud.modules.exam_results.schemas import Preview, QuestionResult, StudentResult
from huipi_cloud.modules.exam_results.xlsx import export, letter


def sample():
    mapping, question = uuid4(), uuid4()
    student = StudentResult(
        participation_id=uuid4(),
        roster_entry_id=uuid4(),
        student_id=uuid4(),
        student_name='=HYPERLINK("https://invalid")',
        student_code="000007",
        exam_number="001",
        class_id=uuid4(),
        class_name="合成1班",
        status="complete",
        variant="A",
        reviewed_count=1,
        required_count=1,
        subtotal="0.00",
        total="0.00",
        max_points="10.00",
        questions=[
            QuestionResult(
                mapping_id=mapping,
                question_id=question,
                number=1,
                max_points="10.00",
                status="reviewed",
                points="0.00",
            )
        ],
    )
    pending = student.model_copy(
        deep=True,
        update=dict(
            participation_id=uuid4(),
            student_id=uuid4(),
            status="incomplete",
            subtotal=None,
            total=None,
            reviewed_count=0,
            questions=[
                QuestionResult(
                    mapping_id=mapping,
                    question_id=question,
                    number=1,
                    max_points="10.00",
                    status="abstained",
                )
            ],
        ),
    )
    return Preview(
        session_id=uuid4(),
        event_id=uuid4(),
        event_title="合成考试",
        session_title="数学",
        roster_revision_id=uuid4(),
        generated_at=datetime.now(UTC),
        snapshot_sha256="a" * 64,
        classes=[],
        students=[student, pending],
        offset=0,
        limit=20,
        total_students=2,
        has_more=False,
    )


def test_xlsx_preserves_real_zero_blank_ids_and_no_formulas():
    with ZipFile(BytesIO(export(sample()))) as z:
        assert z.testzip() is None and len(z.namelist()) == 8
        root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        cells = {c.attrib["r"]: c for c in root.findall(".//s:c", ns)}
        assert cells["E2"].attrib["t"] == "inlineStr"
        assert cells["D2"].find("s:is/s:t", ns).text == "000007"
        assert cells["H2"].attrib["t"] == "n" and Decimal(cells["H2"].find("s:v", ns).text) == 0
        assert "H3" not in cells and "L3" not in cells  # unknowns aren't zero
        assert not root.findall(".//s:f", ns)
        assert root.find("s:sheetProtection", ns) is not None
        assert b"External" not in z.read("xl/_rels/workbook.xml.rels")


def test_distinct_mapping_ids_are_not_joined_by_question_number():
    value = sample()
    second = value.students[1]
    second.variant = "B"
    second.questions[0].mapping_id = uuid4()
    with ZipFile(BytesIO(export(value))) as z:
        data = z.read("xl/worksheets/sheet1.xml").decode()
        assert "A卷·第1题" in data and "B卷·第1题" in data


@pytest.mark.parametrize("number,label", [(1, "A"), (26, "Z"), (27, "AA"), (200, "GR")])
def test_column_addresses(number, label):
    assert letter(number) == label
