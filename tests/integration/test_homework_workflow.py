"""Real JWT, PostgreSQL and private S3 homework identity/attempt regression."""
# ruff: noqa: F811

import asyncio
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import text
from test_teacher_authorization import auth_api, create, question  # noqa: F401

from huipi_cloud.modules.copilot.protocol import CopilotError
from huipi_cloud.modules.copilot.tools import BusinessTools, validate_arguments
from huipi_cloud.modules.organization.models import (
    AcademicTerm,
    AcademicYear,
    AdministrativeClass,
    ClassEnrollment,
    GradeCohort,
    Student,
    Subject,
)


@pytest.fixture
async def homework(auth_api):
    ctx = auth_api
    async with ctx.factory() as db, db.begin():
        year = AcademicYear(
            code=f"Y{uuid4().hex[:8]}",
            display_name="合成学年",
            start_date=date(2026, 1, 1),
            end_date=date(2027, 12, 31),
            status="active",
        )
        cohort = GradeCohort(code=f"C{uuid4().hex[:8]}", display_name="合成届", intake_year=2025)
        subject = Subject(code=f"M{uuid4().hex[:8]}", display_name="数学", is_active=True)
        db.add_all([year, cohort, subject])
        await db.flush()
        term = AcademicTerm(
            academic_year_id=year.id,
            term_no=1,
            code="上学期",
            start_date=date(2026, 1, 1),
            end_date=date(2027, 12, 31),
        )
        classes = [
            AdministrativeClass(
                grade_cohort_id=cohort.id,
                code=f"A{uuid4().hex[:8]}",
                display_name=f"合成{i}班",
                status="active",
            )
            for i in (1, 2)
        ]
        db.add_all([term, *classes])
        await db.flush()
        students = [
            Student(school_student_code=f"S{uuid4().hex[:8]}", name="合成同名", is_active=True)
            for _ in range(2)
        ]
        db.add_all(students)
        await db.flush()
        for student, classroom in zip(students, classes, strict=True):
            db.add(
                ClassEnrollment(
                    student_id=student.id,
                    administrative_class_id=classroom.id,
                    valid_from=date(2026, 1, 1),
                    source="manual",
                )
            )
        await db.flush()
    ctx.classes, ctx.term, ctx.subject, ctx.students = classes, term, subject, students
    try:
        yield ctx
    finally:
        async with ctx.factory() as db, db.begin():
            await db.execute(
                text(
                    "TRUNCATE homework_attempts, homework_participants, homework_classes, "
                    "homework_offerings, homework_teaching_assignments CASCADE"
                )
            )
            await db.execute(
                text(
                    "TRUNCATE class_enrollments, students, administrative_classes, subjects, "
                    "grade_cohorts, academic_terms, academic_years CASCADE"
                )
            )


@pytest.mark.anyio
async def test_teacher_classes_roster_reupload_and_cross_class(homework):
    c = homework
    assert (
        await c.client.get("/api/v1/homework/admin/teaching-assignments", headers=c.headers("a"))
    ).status_code == 403
    admin_options = await c.client.get(
        "/api/v1/homework/admin/teaching-assignments", headers=c.headers("admin")
    )
    assert admin_options.status_code == 200
    assert {str(x.id) for x in c.classes}.issubset(
        {x["id"] for x in admin_options.json()["classes"]}
    )
    aid = await create(c)
    qid = await question(c, aid)
    assert (
        await c.client.put(
            f"/api/v1/questions/{qid}/answer-key",
            headers=c.headers("a"),
            json={"answer_content": "2"},
        )
    ).status_code == 200
    assert (
        await c.client.put(
            f"/api/v1/questions/{qid}/rubric",
            headers=c.headers("a"),
            json={"criteria": [{"description": "正确", "points": "2.00", "sort_order": 1}]},
        )
    ).status_code == 200
    assert (
        await c.client.post(f"/api/v1/assignments/{aid}/publish", headers=c.headers("a"))
    ).status_code == 200
    for classroom in c.classes:
        body = dict(
            teacher_id=str(c.users["a"].id),
            class_id=str(classroom.id),
            term_id=str(c.term.id),
            subject_id=str(c.subject.id),
        )
        assert (
            await c.client.post(
                "/api/v1/homework/admin/teaching-assignments", headers=c.headers("a"), json=body
            )
        ).status_code == 403
        response = await c.client.post(
            "/api/v1/homework/admin/teaching-assignments", headers=c.headers("admin"), json=body
        )
        assert response.status_code == 200, response.text
    options = await c.client.get("/api/v1/homework/teaching-assignments", headers=c.headers("a"))
    assert len(options.json()["items"]) == 2
    body = dict(
        term_id=str(c.term.id),
        subject_id=str(c.subject.id),
        class_ids=[str(x.id) for x in c.classes],
        due_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
    )
    assigned = await c.client.post(
        f"/api/v1/assignments/{aid}/homework", headers=c.headers("a"), json=body
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["student_count"] == 2
    view = await c.client.get(f"/api/v1/assignments/{aid}/homework", headers=c.headers("a"))
    assert view.status_code == 200, view.text
    rows = view.json()["students"]
    assert len(rows) == 2 and rows[0]["student_name"] == rows[1]["student_name"]
    assert all(r["status"] == "not_submitted" for r in rows)
    pid = rows[0]["participant_id"]
    request_id = str(uuid4())

    async def upload(rid, content):
        return await c.client.post(
            f"/api/v1/homework/participants/{pid}/attempts",
            headers=c.headers("a"),
            data={"request_id": rid},
            files={"file": ("synthetic.pdf", content, "application/pdf")},
        )

    first = await upload(request_id, b"%PDF-1.4\nfirst\n%%EOF")
    assert first.status_code == 200, first.text
    replay = await upload(request_id, b"%PDF-1.4\nfirst\n%%EOF")
    assert replay.status_code == 200 and replay.json()["created"] is False
    conflict = await upload(request_id, b"%PDF-1.4\nother\n%%EOF")
    assert conflict.status_code == 409
    second = await upload(str(uuid4()), b"%PDF-1.4\nsecond\n%%EOF")
    assert second.status_code == 200, second.text
    assert second.json()["version"] == 2
    history = await c.client.get(
        f"/api/v1/homework/participants/{pid}/attempts", headers=c.headers("a")
    )
    assert [x["is_current"] for x in history.json()["items"]] == [True, False]
    new = await c.client.get(f"/api/v1/assignments/{aid}/homework", headers=c.headers("a"))
    row = next(r for r in new.json()["students"] if r["participant_id"] == pid)
    assert row["current_submission_id"] == second.json()["submission_id"]
    assert (
        await c.client.get(
            f"/api/v1/submissions/{second.json()['submission_id']}", headers=c.headers("b")
        )
    ).status_code == 404
    assert (
        await c.client.get(f"/api/v1/assignments/{aid}/submissions", headers=c.headers("a"))
    ).json() == []
    copilot = BusinessTools(c.factory, aid)
    summary = await copilot.execute(
        "get_assignment_summary", validate_arguments("get_assignment_summary", "{}")
    )
    assert summary["submission_count"] == 0
    with pytest.raises(CopilotError, match="resource_not_found_in_scope"):
        await copilot.execute(
            "get_submission_processing_status",
            validate_arguments(
                "get_submission_processing_status",
                '{"submission_id":"' + second.json()["submission_id"] + '"}',
            ),
        )
    assert (
        await c.client.post(
            f"/api/v1/submissions/{first.json()['submission_id']}/answer-reviews/questions/{qid}",
            headers=c.headers("a"),
            json={},
        )
    ).status_code == 409
    member = f"/api/v1/assignments/{aid}/members/{c.users['b'].id}"
    assert (
        await c.client.put(member, headers=c.headers("admin"), json={"can_write": True})
    ).status_code == 200
    other_class = next(r["class_id"] for r in rows if r["participant_id"] != pid)
    grant = await c.client.post(
        "/api/v1/homework/admin/teaching-assignments",
        headers=c.headers("admin"),
        json=dict(
            teacher_id=str(c.users["b"].id),
            class_id=other_class,
            term_id=str(c.term.id),
            subject_id=str(c.subject.id),
        ),
    )
    assert grant.status_code == 200
    limited = await c.client.get(f"/api/v1/assignments/{aid}/homework", headers=c.headers("b"))
    assert limited.status_code == 200 and len(limited.json()["students"]) == 1
    assert limited.json()["students"][0]["class_id"] == other_class
    assert (
        await c.client.get(
            f"/api/v1/submissions/{second.json()['submission_id']}/file", headers=c.headers("b")
        )
    ).status_code == 404
    assert (
        await c.client.get(
            f"/api/v1/submissions/{second.json()['submission_id']}", headers=c.headers("admin")
        )
    ).status_code == 404
    assert (
        await c.client.delete(
            f"/api/v1/homework/admin/teaching-assignments/{grant.json()['id']}",
            headers=c.headers("admin"),
        )
    ).status_code == 200
    assert (
        await c.client.get(f"/api/v1/assignments/{aid}/homework", headers=c.headers("b"))
    ).status_code == 404
    other_pid = next(r["participant_id"] for r in rows if r["participant_id"] != pid)
    image = BytesIO()
    Image.new("RGB", (16, 16), "white").save(image, format="PNG")
    image_bytes = image.getvalue()
    image_submission = await c.client.post(
        f"/api/v1/homework/participants/{other_pid}/attempts",
        headers=c.headers("a"),
        data={"request_id": str(uuid4())},
        files={"file": ("synthetic.png", image_bytes, "image/png")},
    )
    assert image_submission.status_code == 200
    image_readback = await c.client.get(
        f"/api/v1/submissions/{image_submission.json()['submission_id']}/file",
        headers=c.headers("a"),
    )
    assert image_readback.status_code == 200 and image_readback.content == image_bytes
    assert image_submission.json()["submission_id"] != second.json()["submission_id"]
    concurrent = await asyncio.gather(
        upload(str(uuid4()), b"%PDF-1.4\nthird\n%%EOF"),
        upload(str(uuid4()), b"%PDF-1.4\nfourth\n%%EOF"),
    )
    assert all(r.status_code == 200 for r in concurrent), [r.text for r in concurrent]
    assert {r.json()["version"] for r in concurrent} == {3, 4}
    history = await c.client.get(
        f"/api/v1/homework/participants/{pid}/attempts", headers=c.headers("a")
    )
    assert [r["version"] for r in history.json()["items"]] == [4, 3, 2, 1]
    assert sum(r["is_current"] for r in history.json()["items"]) == 1
