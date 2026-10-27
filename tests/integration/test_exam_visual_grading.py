"""Isolated PostgreSQL/S3 integration; the provider here is explicitly synthetic."""
# ruff: noqa: F401, F811

import json
from decimal import Decimal
from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_exam_results import (
    SyntheticScoreProvider,
    allow,
    auth_api,
    base,
    consume,
    detail,
    exam,
    marking,
    papers,
    parse_context,
    read,
    results,
    roster,
    scan_context,
    score,
    script_context,
    start,
    target,
)

from huipi_cloud.infrastructure.grading.provider import ProviderUsage, TextGradingResponse
from huipi_cloud.modules.exam_grading import service as g
from huipi_cloud.modules.exam_grading.runtime import ScoringWorker


class SyntheticVisualProvider:
    provider_name = "openai_compatible"
    model_id = "synthetic-visual-test-only"
    generation_config_digest = "b" * 64

    async def complete_visual(self, *, system_prompt, user_prompt, images):
        assert "不能伪造文字引用" in user_prompt
        assert len(images) == 2 and all(image.startswith(b"\x89PNG") for image in images)
        self.received_images = [len(image) for image in images]
        data = json.loads(user_prompt)
        score = sum((Decimal(item["points"]) for item in data["rubric"]), Decimal("0.00"))
        reply = dict(
            schema_version="1.0",
            outcome="candidate",
            total_score=str(score),
            criterion_scores=[
                dict(
                    criterion_id=item["criterion_id"],
                    awarded_points=item["points"],
                    rationale="合成图像评分协议测试",
                    evidence_references=[
                        dict(kind="image_region", region_index=0, description="该区域可见作答")
                    ],
                )
                for item in data["rubric"]
            ],
        )
        return TextGradingResponse(json.dumps(reply), self.model_id, 1, ProviderUsage(10, 5))


def enable_visual(monkeypatch):
    monkeypatch.setattr(g.settings, "grading_enabled", True)
    monkeypatch.setattr(g.settings, "grading_visual_enabled", True)


async def choose(c):
    d = await detail(c)
    assert len(d["source_pages"]) >= 2
    regions = [
        dict(scan_page_id=p["scan_page_id"], x0=1000, y0=1000, x1=8500, y1=8500)
        for p in d["source_pages"][:2]
    ]
    body = dict(request_id=str(uuid4()), expected_revision=0, action="select", regions=regions)
    r = await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/visual-selection", json=body)
    assert r.status_code == 200, r.text
    replay = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/visual-selection", json=body
    )
    assert replay.status_code == 200 and replay.json() == r.json()
    d = await detail(c)
    assert d["visual_selection"]["active"] and len(d["visual_selection"]["regions"]) == 2
    return d


@pytest.mark.anyio
async def test_visual_selection_scoring_review_and_results(results, monkeypatch):
    c = results
    enable_visual(monkeypatch)
    selected = await choose(c)
    request = dict(request_id=str(uuid4()), expected_selection_revision=1)
    r = await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/visual-scoring", json=request)
    assert r.status_code == 202, r.text
    assert (
        await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/visual-scoring", json=request)
    ).json() == r.json()
    provider = SyntheticVisualProvider()
    worker = ScoringWorker(
        c.factory,
        c.storage,
        provider,
        c.config.model_copy(update={"grading_enabled": True, "grading_visual_enabled": True}),
    )
    assert await worker.once()
    assert len(provider.received_images) == 2
    d = await detail(c)
    assert d["scoring_status"] == "succeeded", d
    assert d["proposal"] is None and d["visual_proposal"]["grading_status"] == "completed_candidate"
    assert d["visual_grade_input"]["selection_id"] == selected["visual_selection"]["id"]
    assert (
        d["visual_proposal"]["criterion_scores"][0]["evidence_references"][0]["kind"]
        == "image_region"
    )
    before = target(c, (await read(c)).json())
    assert before["subtotal"] is None and before["total"] is None
    review = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        payload=dict(
            schema_name="huipi.grading.visual-review",
            schema_version="2.0",
            review_status="accepted",
            reviewer_ref="teacher_" + c.users["a"].id.hex,
        ),
    )
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/visual-score-review", json=review
    )
    assert r.status_code == 200, r.text
    d = await detail(c)
    assert d["visual_history"][0]["review_status"] == "accepted"
    after = target(c, (await read(c)).json())
    assert after["subtotal"] == "10.00" and after["total"] is None
    assert any(
        q["evidence_source"] == "visual" and q["points"] == "10.00" for q in after["questions"]
    )
    exported = await c.client.get(f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx")
    assert exported.status_code == 200
    with ZipFile(BytesIO(exported.content)) as archive:
        assert b"10.00" in archive.read("xl/worksheets/sheet1.xml")
    candidate = d["visual_proposal"]["criterion_scores"]
    revised = dict(
        request_id=str(uuid4()),
        expected_revision=1,
        payload=dict(
            schema_name="huipi.grading.visual-review",
            schema_version="2.0",
            review_status="revised",
            reviewer_ref="teacher_" + c.users["a"].id.hex,
            review_reason_codes=["incorrect_partial_credit"],
            criterion_reviews=[
                dict(
                    criterion_id=item["criterion_id"],
                    reviewed_points="3.00" if index == 0 else item["awarded_points"],
                    **(
                        dict(
                            rationale="教师核对图片后修订过程分",
                            evidence_references=item["evidence_references"],
                        )
                        if index == 0
                        else {}
                    ),
                )
                for index, item in enumerate(candidate)
            ],
        ),
    )
    response = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/visual-score-review", json=revised
    )
    assert response.status_code == 200, response.text
    d = await detail(c)
    assert [h["review_status"] for h in d["visual_history"]] == ["accepted", "revised"]
    after_revision = target(c, (await read(c)).json())
    assert after_revision["subtotal"] == "8.00" and after_revision["total"] is None
    exported = await c.client.get(f"/api/v1/exam-sessions/{c.session['id']}/results-preview.xlsx")
    with ZipFile(BytesIO(exported.content)) as archive:
        assert b"8.00" in archive.read("xl/worksheets/sheet1.xml")
    clear = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/visual-selection",
        json=dict(request_id=str(uuid4()), expected_revision=1, action="clear", regions=[]),
    )
    assert clear.status_code == 200, clear.text
    stale = target(c, (await read(c)).json())
    assert stale["subtotal"] is None and stale["total"] is None


@pytest.mark.anyio
async def test_visual_cross_page_and_revision_checks(results, monkeypatch):
    c = results
    enable_visual(monkeypatch)
    wrong = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        action="select",
        regions=[dict(scan_page_id=str(uuid4()), x0=100, y0=100, x1=9000, y1=9000)],
    )
    r = await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/visual-selection", json=wrong)
    assert r.status_code == 404
    await choose(c)
    wrong["request_id"] = str(uuid4())
    r = await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/visual-selection", json=wrong)
    assert r.status_code == 409
    denied = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/visual-scoring",
        headers=c.headers("b"),
        json=dict(request_id=str(uuid4()), expected_selection_revision=1),
    )
    assert denied.status_code == 404
    async with c.factory() as db, db.begin():
        rows = (await db.execute(select(g.W.c.id).where(g.W.c.id == UUID(c.wid)))).all()
        assert len(rows) == 1


@pytest.mark.anyio
async def test_cleared_visual_selection_does_not_revive_old_text_score(results, monkeypatch):
    c = results
    enable_visual(monkeypatch)
    await score(c)
    assert target(c, (await read(c)).json())["subtotal"] == "10.00"
    await choose(c)
    assert target(c, (await read(c)).json())["subtotal"] is None
    response = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/visual-selection",
        json=dict(request_id=str(uuid4()), expected_revision=1, action="clear", regions=[]),
    )
    assert response.status_code == 200, response.text
    assert target(c, (await read(c)).json())["subtotal"] is None
    response = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
        json=dict(request_id=str(uuid4()), expected_answer_revision=1),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "exam_visual_requires_text_confirmation"

    d = await detail(c)
    node = next(n for n in d["nodes"] if n["page_index"] == 0)
    response = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/answer-review",
        json=dict(
            request_id=str(uuid4()),
            manual_attribution=True,
            expected_revision=1,
            payload=dict(
                decision="response_present",
                response_regions=[
                    dict(
                        content_pointer=node["content_pointer"],
                        text_start=0,
                        text_end=len(node["text"]),
                    )
                ],
                reason_codes=["student_work_visible"],
                reviewer_ref="teacher_" + c.users["a"].id.hex,
            ),
        ),
    )
    assert response.status_code == 200, response.text
    response = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
        json=dict(request_id=str(uuid4()), expected_answer_revision=2),
    )
    assert response.status_code == 202, response.text
    assert await c.grade_worker.once()
    new = await detail(c)
    assert new["proposal"] and not new["visual_proposal"]
    assert target(c, (await read(c)).json())["subtotal"] is None  # needs new human review


@pytest.mark.anyio
async def test_visual_history_is_immutable_and_blocks_destructive_downgrade(
    results, monkeypatch, postgres_engine
):
    from test_organization_migration import alembic

    c = results
    enable_visual(monkeypatch)
    selected = await choose(c)
    selection_id = UUID(selected["visual_selection"]["id"])
    async with c.factory() as db:
        head_before = await db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(DBAPIError):
        async with c.factory() as db, db.begin():
            await db.execute(
                text("UPDATE exam_visual_selections SET active=false WHERE id=:id"),
                {"id": selection_id},
            )
    result = await alembic(postgres_engine.url, "downgrade", "b17a93f7b018", success=False)
    assert result.returncode != 0 and "禁止破坏性降级" in result.stderr
    async with c.factory() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == head_before
        assert await db.scalar(
            text("SELECT active FROM exam_visual_selections WHERE id=:id"),
            {"id": selection_id},
        )


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["selection", "revoke"])
async def test_visual_worker_rechecks_source_and_authority(results, monkeypatch, change):
    import asyncio

    from huipi_cloud.modules.resource_security import service as security

    c = results
    enable_visual(monkeypatch)
    await choose(c)
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/visual-scoring",
        json=dict(request_id=str(uuid4()), expected_selection_revision=1),
    )
    assert r.status_code == 202, r.text
    entered, release = asyncio.Event(), asyncio.Event()
    provider = SyntheticVisualProvider()
    original = provider.complete_visual

    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    provider.complete_visual = blocked
    worker = ScoringWorker(
        c.factory,
        c.storage,
        provider,
        c.config.model_copy(update={"grading_enabled": True, "grading_visual_enabled": True}),
    )
    running = asyncio.create_task(worker.once())
    await asyncio.wait_for(entered.wait(), 10)
    try:
        if change == "selection":
            d = await detail(c)
            page = d["source_pages"][0]["scan_page_id"]
            changed = await c.client.post(
                f"/api/v1/exam-grading-workspaces/{c.wid}/visual-selection",
                json=dict(
                    request_id=str(uuid4()),
                    expected_revision=1,
                    action="select",
                    regions=[dict(scan_page_id=page, x0=2000, y0=2000, x1=8000, y1=8000)],
                ),
            )
            assert changed.status_code == 200, changed.text
        else:
            async with c.factory() as db:
                await security.revoke_grant(
                    db, c.principals["admin"], uuid4(), grant_id=UUID(c.mark_grant["grant_id"])
                )
        release.set()
        await asyncio.wait_for(running, 15)
    finally:
        release.set()
        await asyncio.gather(running, return_exceptions=True)
    async with c.factory() as db:
        assert not (await db.execute(select(g.GP))).first()
        assert await db.scalar(select(g.S.c.status)) == "invalidated"


@pytest.mark.anyio
async def test_visual_abstention_never_creates_score(results, monkeypatch):
    c = results
    enable_visual(monkeypatch)
    await choose(c)
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/visual-scoring",
        json=dict(request_id=str(uuid4()), expected_selection_revision=1),
    )
    assert r.status_code == 202
    provider = SyntheticVisualProvider()

    async def abstain(**kwargs):
        assert len(kwargs["images"]) == 2
        return TextGradingResponse(
            json.dumps(
                dict(
                    schema_version="1.0",
                    outcome="abstain",
                    total_score=None,
                    criterion_scores=[],
                    abstention_reason="图像模糊无法判断",
                )
            ),
            provider.model_id,
            1,
            ProviderUsage(10, 5),
        )

    provider.complete_visual = abstain
    worker = ScoringWorker(
        c.factory,
        c.storage,
        provider,
        c.config.model_copy(update={"grading_enabled": True, "grading_visual_enabled": True}),
    )
    assert await worker.once()
    d = await detail(c)
    assert d["visual_proposal"]["grading_status"] == "grading_abstained"
    before = target(c, (await read(c)).json())
    assert before["subtotal"] is None and before["total"] is None
    review = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        payload=dict(
            schema_name="huipi.grading.visual-review",
            schema_version="2.0",
            review_status="regrading_required",
            reviewer_ref="teacher_" + c.users["a"].id.hex,
            review_reason_codes=["source_quality_problem"],
        ),
    )
    response = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/visual-score-review", json=review
    )
    assert response.status_code == 200, response.text
    after = target(c, (await read(c)).json())
    assert after["subtotal"] is None and after["total"] is None


def visual_mixed_scan_pdf():
    """Synthetic hand-drawn Chinese, arithmetic and triangle, mixed across four pages."""
    from PIL import Image, ImageDraw, ImageFont

    pages = []
    font = ImageFont.truetype("DejaVuSans.ttf", 20)
    for label, sheet in [("N001", 2), ("N002", 1), ("N001", 1), ("N002", 2)]:
        image = Image.new("RGB", (800, 1100), "white")
        d = ImageDraw.Draw(image)
        d.text((45, 45), f"SYNTHETIC TEST {label} PAGE {sheet}", font=font, fill="black")
        # Hand-drawn 二 and 四: intentional pen-like uneven strokes, not student data.
        if sheet == 1:
            for x in (100, 260):
                d.line([(x + 5, 165), (x + 35, 163), (x + 62, 166)], fill="black", width=6)
                d.line([(x, 205), (x + 28, 202), (x + 70, 204)], fill="black", width=7)
            d.line([(193, 170), (193, 205)], fill="black", width=6)
            d.line([(176, 187), (211, 187)], fill="black", width=6)
            d.line([(380, 177), (424, 175)], fill="black", width=6)
            d.line([(380, 194), (424, 192)], fill="black", width=6)
            d.text(
                (100, 260),
                "2  +  2  =",
                font=ImageFont.truetype("DejaVuSans-Oblique.ttf", 48),
                fill="black",
            )
        else:
            d.line(
                [(120, 155), (120, 220), (180, 220), (180, 155), (120, 155)], fill="black", width=6
            )
            d.line([(145, 166), (140, 193), (132, 202)], fill="black", width=5)
            d.line([(157, 165), (157, 195), (169, 195)], fill="black", width=5)
            d.text(
                (225, 155), "4", font=ImageFont.truetype("DejaVuSans-Oblique.ttf", 65), fill="black"
            )
            d.line([(160, 460), (310, 270), (460, 460), (160, 460)], fill="black", width=5)
            d.text((145, 465), "A", font=font, fill="black")
            d.text((305, 235), "B", font=font, fill="black")
            d.text((465, 465), "C", font=font, fill="black")
        pages.append(image)
    out = BytesIO()
    try:
        pages[0].save(out, format="PDF", save_all=True, append_images=pages[1:], resolution=100)
        return out.getvalue()
    finally:
        for page in pages:
            page.close()
