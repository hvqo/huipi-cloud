from types import SimpleNamespace as NS
from uuid import uuid4

from huipi_cloud.modules.exam_results.final_service import statistics


def test_statistics_keep_versions_separate_and_never_count_absence_as_zero():
    group_a, group_b, cls = uuid4(), uuid4(), uuid4()
    question = uuid4()
    students = [
        NS(
            class_id=cls,
            class_name="合成班",
            attendance_status="present",
            paper_version_id=group_a,
            variant="A",
            max_points="100.00",
            total="80.00",
            questions=[NS(mapping_id=question, number=1, max_points="10.00", points="8.00")],
        ),
        NS(
            class_id=cls,
            class_name="合成班",
            attendance_status="present",
            paper_version_id=group_b,
            variant="B",
            max_points="50.00",
            total="25.00",
            questions=[],
        ),
        NS(
            class_id=cls,
            class_name="合成班",
            attendance_status="absent",
            paper_version_id=None,
            variant=None,
            max_points=None,
            total=None,
            questions=[],
        ),
    ]
    value = statistics(NS(students=students))
    assert value["classes"][0]["completion_rate"] == "66.67"
    assert value["classes"][0]["absent"] == 1
    groups = {p["variant"]: p for p in value["papers"]}
    assert (
        groups["A"]["average"] == "80.00" and groups["A"]["questions"][0]["score_rate"] == "80.00"
    )
    assert groups["B"]["average"] == "25.00" and groups["B"]["distribution"]["0-59%"] == 1
