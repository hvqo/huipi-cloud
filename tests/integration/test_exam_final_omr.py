"""Publish only real OMR decisions after explicit teacher confirmation."""

# ruff: noqa: F401, F811
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from test_exam_final_results import prepare, root, set_attendance, step
from test_exam_omr import (
    auth_api,
    base_papers,
    exam,
    omr_scan,
    papers,
    roster,
    scan_context,
)
from test_exam_omr import (
    test_real_omr_teacher_confirmation_and_score_export as omr_flow,
)

from huipi_cloud.modules.exam_results import access
from huipi_cloud.modules.exam_results import final_service as final
from huipi_cloud.modules.exam_results.permissions import RESULT_MANAGER_ROLE
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityScope


@pytest.mark.anyio
async def test_published_scores_use_teacher_confirmed_real_omr(omr_scan):
    c = omr_scan
    await omr_flow(c)
    async with c.factory() as db:
        await final.enable_operator(
            db, c.users["admin"].id, c.users["admin"].id, uuid4(), "合成成绩部署"
        )
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        grant = await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=RESULT_MANAGER_ROLE,
            scope_id=scope,
        )
    for cls in c.classes:
        async with c.factory() as db:
            await access.grant_class(
                db,
                c.principals["admin"],
                uuid4(),
                grant_id=UUID(grant["grant_id"]),
                session_id=UUID(c.session["id"]),
                class_id=cls.id,
                export_allowed=True,
            )
    await set_attendance(c)
    created = await prepare(c)
    assert created.status_code == 200, created.text
    vid = created.json()["version_id"]
    assert (await step(c, vid, "approve", 1)).status_code == 200
    assert (await step(c, vid, "publish", 2)).status_code == 200
    published = await c.client.get(root(c) + f"/versions/{vid}")
    assert published.status_code == 200, published.text
    student = next(
        s
        for s in published.json()["result"]["students"]
        if s["participation_id"] == c.results_participation
    )
    assert student["total"] == "10.00"
    assert all(q["omr_decision_id"] and q["review_id"] is None for q in student["questions"])
