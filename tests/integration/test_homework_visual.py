"""Homework image grading against dedicated PostgreSQL and private test S3."""
# ruff: noqa: F401, F811

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_homework_workflow import auth_api, create, homework, question

from huipi_cloud.infrastructure.grading.provider import ProviderUsage, TextGradingResponse
from huipi_cloud.modules.homework import visual_service


class ImageScoreProvider:
    provider_name = "openai_compatible"
    model_id = "synthetic-visual-tests-only"
    generation_config_digest = "b" * 64

    async def complete_visual(self, *, system_prompt, user_prompt, images):
        assert len(images) == 1 and images[0].startswith(b"\x89PNG")
        data = json.loads(user_prompt)
        scores = [
            dict(
                criterion_id=item["criterion_id"],
                awarded_points=item["points"],
                rationale="合成数据测试：区域中可以看到学生作答",
                evidence_references=[
                    dict(kind="image_region", region_index=0, description="所选作答区域")
                ],
            )
            for item in data["rubric"]
        ]
        return TextGradingResponse(
            json.dumps(
                dict(
                    outcome="candidate",
                    total_score=str(sum(Decimal(s["awarded_points"]) for s in scores)),
                    criterion_scores=scores,
                )
            ),
            self.model_id,
            1,
            ProviderUsage(10, 5),
        )


def synthetic_png():
    image = Image.new("RGB", (900, 1000), "white")
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 52)
    except OSError:
        font = ImageFont.load_default()
    ImageDraw.Draw(image).text((120, 200), "1. 1 + 1 = 2", fill="black", font=font)
    content = BytesIO()
    image.save(content, format="PNG")
    return content.getvalue()


async def setup_assignment(c):
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
            json={"criteria": [{"description": "计算正确", "points": "2.00", "sort_order": 1}]},
        )
    ).status_code == 200
    assert (
        await c.client.post(f"/api/v1/assignments/{aid}/publish", headers=c.headers("a"))
    ).status_code == 200
    for classroom in c.classes:
        r = await c.client.post(
            "/api/v1/homework/admin/teaching-assignments",
            headers=c.headers("admin"),
            json=dict(
                teacher_id=str(c.users["a"].id),
                class_id=str(classroom.id),
                term_id=str(c.term.id),
                subject_id=str(c.subject.id),
            ),
        )
        assert r.status_code == 200, r.text
    r = await c.client.post(
        f"/api/v1/assignments/{aid}/homework",
        headers=c.headers("a"),
        json=dict(
            term_id=str(c.term.id),
            subject_id=str(c.subject.id),
            class_ids=[str(x.id) for x in c.classes],
            due_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        ),
    )
    assert r.status_code == 200, r.text
    r = await c.client.get(f"/api/v1/assignments/{aid}/homework", headers=c.headers("a"))
    rows = r.json()["students"]
    assert len(rows) == 2 and rows[0]["student_name"] == rows[1]["student_name"]
    return aid, qid, rows


async def upload(c, participant):
    r = await c.client.post(
        f"/api/v1/homework/participants/{participant}/attempts",
        headers=c.headers("a"),
        data={"request_id": str(uuid4())},
        files={"file": ("synthetic.png", synthetic_png(), "image/png")},
    )
    assert r.status_code == 200, r.text
    return r.json()["submission_id"]


@pytest.mark.anyio
async def test_visual_image_review_and_resubmission(homework, monkeypatch):
    c = homework
    monkeypatch.setattr(visual_service.settings, "grading_enabled", True)
    monkeypatch.setattr(visual_service.settings, "grading_visual_enabled", True)
    monkeypatch.setattr(
        visual_service, "build_text_grading_provider", lambda _: ImageScoreProvider()
    )
    aid, qid, rows = await setup_assignment(c)
    sid = await upload(c, rows[0]["participant_id"])
    base = f"/api/v1/homework/submissions/{sid}/questions/{qid}/visual"
    page = f"/api/v1/homework/submissions/{sid}/visual-pages/0"
    assert (await c.client.get(page, headers=c.headers("a"))).content.startswith(b"\xff\xd8")
    assert (await c.client.get(page, headers=c.headers("b"))).status_code in (403, 404)
    selection = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        regions=[dict(page_index=0, x0=200, y0=800, x1=8000, y1=8000)],
    )
    r = await c.client.post(base + "/selection", headers=c.headers("a"), json=selection)
    assert r.status_code == 200, r.text
    assert (
        await c.client.post(base + "/selection", headers=c.headers("a"), json=selection)
    ).json()["created"] is False
    score = dict(
        request_id=str(uuid4()), expected_selection_revision=1, include_confirmed_text=False
    )
    r = await c.client.post(base + "/score", headers=c.headers("a"), json=score)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "succeeded"
    replay = await c.client.post(base + "/score", headers=c.headers("a"), json=score)
    assert replay.status_code == 200 and replay.json()["created"] is False
    detail = await c.client.get(base, headers=c.headers("a"))
    assert detail.status_code == 200, detail.text
    proposal = detail.json()["proposal"]
    assert proposal["criterion_scores"][0]["evidence_references"][0]["kind"] == "image_region"
    review = dict(request_id=str(uuid4()), expected_revision=0, review_status="accepted")
    r = await c.client.post(base + "/review", headers=c.headers("a"), json=review)
    assert r.status_code == 200, r.text
    assert (
        await c.client.post(base + "/review", headers=c.headers("a"), json=review)
    ).status_code == 200
    view = (
        await c.client.get(f"/api/v1/assignments/{aid}/homework", headers=c.headers("a"))
    ).json()
    row = next(
        row for row in view["students"] if row["participant_id"] == rows[0]["participant_id"]
    )
    assert row["reviewed_count"] == 1
    rejected = dict(request_id=str(uuid4()), expected_revision=1, review_status="rejected")
    result = await c.client.post(base + "/review", headers=c.headers("a"), json=rejected)
    assert result.status_code == 200, result.text
    view = (
        await c.client.get(f"/api/v1/assignments/{aid}/homework", headers=c.headers("a"))
    ).json()
    row = next(
        row for row in view["students"] if row["participant_id"] == rows[0]["participant_id"]
    )
    assert row["reviewed_count"] == 0
    accepted_again = dict(request_id=str(uuid4()), expected_revision=2, review_status="accepted")
    result = await c.client.post(base + "/review", headers=c.headers("a"), json=accepted_again)
    assert result.status_code == 200, result.text
    new_sid = await upload(c, rows[0]["participant_id"])
    assert new_sid != sid
    assert (await c.client.get(base, headers=c.headers("a"))).status_code == 409
    view = (
        await c.client.get(f"/api/v1/assignments/{aid}/homework", headers=c.headers("a"))
    ).json()
    row = next(
        row for row in view["students"] if row["participant_id"] == rows[0]["participant_id"]
    )
    assert row["reviewed_count"] == 0
    async with c.factory() as db:
        with pytest.raises(DBAPIError):
            await db.execute(
                text("UPDATE homework_visual_selections SET regions_sha256='" + "a" * 64 + "'")
            )


@pytest.mark.anyio
async def test_new_selection_invalidates_review_and_malformed_inputs(homework, monkeypatch):
    c = homework
    monkeypatch.setattr(visual_service.settings, "grading_enabled", True)
    monkeypatch.setattr(visual_service.settings, "grading_visual_enabled", True)
    monkeypatch.setattr(
        visual_service, "build_text_grading_provider", lambda _: ImageScoreProvider()
    )
    aid, qid, rows = await setup_assignment(c)
    sid = await upload(c, rows[0]["participant_id"])
    base = f"/api/v1/homework/submissions/{sid}/questions/{qid}/visual"
    region = dict(page_index=0, x0=1000, y0=1000, x1=9000, y1=9000)
    first = dict(request_id=str(uuid4()), expected_revision=0, regions=[region])
    assert (
        await c.client.post(base + "/selection", headers=c.headers("a"), json=first)
    ).status_code == 200
    malformed = {
        **first,
        "request_id": str(uuid4()),
        "expected_revision": 1,
        "regions": [{**region, "x1": 500}],
    }
    assert (
        await c.client.post(base + "/selection", headers=c.headers("a"), json=malformed)
    ).status_code == 422
    assert (
        await c.client.post(base + "/selection", headers=c.headers("b"), json=first)
    ).status_code in (403, 404)
    score = dict(request_id=str(uuid4()), expected_selection_revision=1)
    assert (
        await c.client.post(base + "/score", headers=c.headers("a"), json=score)
    ).status_code == 200
    review = dict(request_id=str(uuid4()), expected_revision=0, review_status="accepted")
    assert (
        await c.client.post(base + "/review", headers=c.headers("a"), json=review)
    ).status_code == 200
    candidate = (await c.client.get(base, headers=c.headers("a"))).json()["proposal"]
    criterion = candidate["criterion_scores"][0]
    revised = dict(
        request_id=str(uuid4()),
        expected_revision=1,
        review_status="revised",
        criterion_reviews=[
            dict(
                criterion_id=criterion["criterion_id"],
                reviewed_points="1.00",
                rationale="教师根据图片修订",
                evidence_references=criterion["evidence_references"],
            )
        ],
    )
    result = await c.client.post(base + "/review", headers=c.headers("a"), json=revised)
    assert result.status_code == 200, result.text
    latest = (await c.client.get(base, headers=c.headers("a"))).json()["review"]
    assert latest["status"] == "revised" and latest["reviewed_total_score"] == "1.00"
    second = dict(request_id=str(uuid4()), expected_revision=1, regions=[region])
    assert (
        await c.client.post(base + "/selection", headers=c.headers("a"), json=second)
    ).status_code == 200
    view = (
        await c.client.get(f"/api/v1/assignments/{aid}/homework", headers=c.headers("a"))
    ).json()
    assert (
        next(row for row in view["students"] if row["participant_id"] == rows[0]["participant_id"])[
            "reviewed_count"
        ]
        == 0
    )
    assert (
        await c.client.post(base + "/review", headers=c.headers("a"), json=review)
    ).status_code == 409
    assert (
        await c.client.post(base + "/score", headers=c.headers("a"), json=score)
    ).status_code == 409


@pytest.mark.anyio
async def test_reupload_during_model_call_invalidates_result(homework, monkeypatch):
    import asyncio

    c = homework
    monkeypatch.setattr(visual_service.settings, "grading_enabled", True)
    monkeypatch.setattr(visual_service.settings, "grading_visual_enabled", True)
    started, release = asyncio.Event(), asyncio.Event()

    class BlockingProvider(ImageScoreProvider):
        async def complete_visual(self, **kwargs):
            started.set()
            await release.wait()
            return await super().complete_visual(**kwargs)

    monkeypatch.setattr(visual_service, "build_text_grading_provider", lambda _: BlockingProvider())
    _, qid, rows = await setup_assignment(c)
    sid = await upload(c, rows[0]["participant_id"])
    base = f"/api/v1/homework/submissions/{sid}/questions/{qid}/visual"
    selected = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        regions=[dict(page_index=0, x0=500, y0=500, x1=9000, y1=9000)],
    )
    assert (
        await c.client.post(base + "/selection", headers=c.headers("a"), json=selected)
    ).status_code == 200
    score = dict(request_id=str(uuid4()), expected_selection_revision=1)
    task = asyncio.create_task(c.client.post(base + "/score", headers=c.headers("a"), json=score))
    await asyncio.wait_for(started.wait(), 10)
    replay = await c.client.post(base + "/score", headers=c.headers("a"), json=score)
    assert replay.status_code == 200 and replay.json()["status"] == "running"
    await upload(c, rows[0]["participant_id"])
    release.set()
    result = await asyncio.wait_for(task, 10)
    assert result.status_code == 409, result.text
    async with c.factory() as db:
        rows = (
            await db.execute(text("SELECT status, proposal_id FROM homework_visual_jobs"))
        ).all()
        assert rows == [("invalidated", None)]


@pytest.mark.anyio
async def test_mixed_image_and_teacher_confirmed_text(homework, monkeypatch):
    """A synthetic parser fixture proves mixed mode only accepts confirmed text."""
    from test_answer_review_http import payload
    from test_exam_parsing import SyntheticExecutor
    from test_scan_intake import pdf_bytes

    from huipi_cloud.core.config import settings
    from huipi_cloud.modules.parsing import repository as parsing_repository
    from huipi_cloud.workers.parsing import ParsingWorker

    c = homework
    monkeypatch.setattr(visual_service.settings, "grading_enabled", True)
    monkeypatch.setattr(visual_service.settings, "grading_visual_enabled", True)

    class MixedProvider(ImageScoreProvider):
        async def complete_visual(self, **kwargs):
            data = json.loads(kwargs["user_prompt"])
            assert data["confirmed_student_text"]
            assert any("1+1" in value for value in data["confirmed_student_text"])
            return await super().complete_visual(**kwargs)

    monkeypatch.setattr(visual_service, "build_text_grading_provider", lambda _: MixedProvider())
    _, qid, rows = await setup_assignment(c)
    r = await c.client.post(
        f"/api/v1/homework/participants/{rows[0]['participant_id']}/attempts",
        headers=c.headers("a"),
        data={"request_id": str(uuid4())},
        files={"file": ("synthetic.pdf", pdf_bytes(1), "application/pdf")},
    )
    assert r.status_code == 200, r.text
    sid = r.json()["submission_id"]
    worker = ParsingWorker(
        c.factory, SyntheticExecutor(c.storage, text="第1题 学生作答：1+1=2。"), settings
    )
    claimed = await parsing_repository.claim_next_task(c.factory, lease_seconds=180)
    assert claimed is not None and str(claimed.task.submission_id) == sid
    await worker._execute_claim(claimed)
    r = await c.client.post(f"/api/v1/homework/submissions/{sid}/prepare", headers=c.headers("a"))
    assert r.status_code == 200, r.text
    path = f"/api/v1/submissions/{sid}/answer-reviews/questions/{qid}"
    view = await c.client.get(path, headers=c.headers("a"))
    assert view.status_code == 200, view.text
    reviewed = await c.client.post(path, headers=c.headers("a"), json=payload(view.json()))
    assert reviewed.status_code == 201, reviewed.text
    base = f"/api/v1/homework/submissions/{sid}/questions/{qid}/visual"
    selected = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        regions=[dict(page_index=0, x0=500, y0=500, x1=9000, y1=9000)],
    )
    r = await c.client.post(base + "/selection", headers=c.headers("a"), json=selected)
    assert r.status_code == 200, r.text
    r = await c.client.post(
        base + "/score",
        headers=c.headers("a"),
        json=dict(
            request_id=str(uuid4()), expected_selection_revision=1, include_confirmed_text=True
        ),
    )
    assert r.status_code == 200, r.text
    detail = await c.client.get(base, headers=c.headers("a"))
    assert detail.status_code == 200, detail.text
    assert (
        detail.json()["proposal"]["criterion_scores"][0]["evidence_references"][0]["kind"]
        == "image_region"
    )


@pytest.mark.anyio
async def test_invalid_model_reply_and_source_tampering_do_not_create_score(homework, monkeypatch):
    c = homework
    monkeypatch.setattr(visual_service.settings, "grading_enabled", True)
    monkeypatch.setattr(visual_service.settings, "grading_visual_enabled", True)

    class InvalidEvidenceProvider(ImageScoreProvider):
        async def complete_visual(self, **kwargs):
            response = await super().complete_visual(**kwargs)
            content = json.loads(response.content)
            content["criterion_scores"][0]["evidence_references"][0]["region_index"] = 7
            return TextGradingResponse(json.dumps(content), self.model_id, 1, ProviderUsage(10, 5))

    monkeypatch.setattr(
        visual_service, "build_text_grading_provider", lambda _: InvalidEvidenceProvider()
    )
    aid, qid, rows = await setup_assignment(c)
    sid = await upload(c, rows[0]["participant_id"])
    base = f"/api/v1/homework/submissions/{sid}/questions/{qid}/visual"
    selected = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        regions=[dict(page_index=0, x0=500, y0=500, x1=9000, y1=9000)],
    )
    assert (
        await c.client.post(base + "/selection", headers=c.headers("a"), json=selected)
    ).status_code == 200
    req = dict(request_id=str(uuid4()), expected_selection_revision=1)
    response = await c.client.post(base + "/score", headers=c.headers("a"), json=req)
    assert response.status_code == 503
    detail = (await c.client.get(base, headers=c.headers("a"))).json()
    assert detail["job"]["status"] == "failed" and detail["proposal"] is None
    assert (await c.client.post(base + "/score", headers=c.headers("a"), json=req)).json()[
        "status"
    ] == "failed"
    assert (
        await c.client.post(
            base + "/review",
            headers=c.headers("a"),
            json=dict(request_id=str(uuid4()), expected_revision=0, review_status="accepted"),
        )
    ).status_code == 409
    async with c.factory() as db:
        key = await db.scalar(
            text("SELECT object_key FROM submission_files WHERE submission_id=CAST(:id AS uuid)"),
            {"id": sid},
        )
    await c.storage.upload_fileobj(BytesIO(synthetic_png() + b"tamper"), key, "image/png")
    response = await c.client.get(
        f"/api/v1/homework/submissions/{sid}/visual-pages/0", headers=c.headers("a")
    )
    assert response.status_code == 503
    view = (
        await c.client.get(f"/api/v1/assignments/{aid}/homework", headers=c.headers("a"))
    ).json()
    assert (
        next(row for row in view["students"] if row["participant_id"] == rows[0]["participant_id"])[
            "reviewed_count"
        ]
        == 0
    )


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["pdf", "jpeg"])
async def test_supported_document_page_previews(homework, kind):
    from test_scan_intake import pdf_bytes

    c = homework
    _, qid, rows = await setup_assignment(c)
    if kind == "pdf":
        content, mime, pages = pdf_bytes(2), "application/pdf", [0, 1]
    else:
        data = BytesIO()
        Image.new("RGB", (100, 120), "white").save(data, format="JPEG")
        content, mime, pages = data.getvalue(), "image/jpeg", [0]
    result = await c.client.post(
        f"/api/v1/homework/participants/{rows[0]['participant_id']}/attempts",
        headers=c.headers("a"),
        data={"request_id": str(uuid4())},
        files={"file": ("source." + kind, content, mime)},
    )
    assert result.status_code == 200, result.text
    sid = result.json()["submission_id"]
    for index in pages:
        image = await c.client.get(
            f"/api/v1/homework/submissions/{sid}/visual-pages/{index}", headers=c.headers("a")
        )
        assert image.status_code == 200 and image.content.startswith(b"\xff\xd8")
    body = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        regions=[dict(page_index=index, x0=0, y0=0, x1=9000, y1=9000) for index in pages],
    )
    selected = await c.client.post(
        f"/api/v1/homework/submissions/{sid}/questions/{qid}/visual/selection",
        headers=c.headers("a"),
        json=body,
    )
    assert selected.status_code == 200, selected.text


@pytest.mark.anyio
async def test_unclear_image_abstains_without_zero(homework, monkeypatch):
    c = homework
    monkeypatch.setattr(visual_service.settings, "grading_enabled", True)
    monkeypatch.setattr(visual_service.settings, "grading_visual_enabled", True)

    class AbstainingProvider(ImageScoreProvider):
        async def complete_visual(self, **kwargs):
            assert kwargs["images"]
            return TextGradingResponse(
                json.dumps(
                    dict(
                        outcome="abstain",
                        criterion_scores=[],
                        total_score=None,
                        abstention_reason="图像不清晰",
                        reason_codes=[],
                    )
                ),
                self.model_id,
                1,
                ProviderUsage(10, 5),
            )

    monkeypatch.setattr(
        visual_service, "build_text_grading_provider", lambda _: AbstainingProvider()
    )
    aid, qid, rows = await setup_assignment(c)
    sid = await upload(c, rows[0]["participant_id"])
    base = f"/api/v1/homework/submissions/{sid}/questions/{qid}/visual"
    selection = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        regions=[dict(page_index=0, x0=500, y0=500, x1=9000, y1=9000)],
    )
    assert (
        await c.client.post(base + "/selection", headers=c.headers("a"), json=selection)
    ).status_code == 200
    assert (
        await c.client.post(
            base + "/score",
            headers=c.headers("a"),
            json=dict(request_id=str(uuid4()), expected_selection_revision=1),
        )
    ).status_code == 200
    detail = (await c.client.get(base, headers=c.headers("a"))).json()
    assert detail["proposal"]["grading_status"] == "grading_abstained"
    assert detail["proposal"]["total_score"] is None
    bad = dict(request_id=str(uuid4()), expected_revision=0, review_status="accepted")
    assert (
        await c.client.post(base + "/review", headers=c.headers("a"), json=bad)
    ).status_code == 422
    manual = dict(request_id=str(uuid4()), expected_revision=0, review_status="regrading_required")
    assert (
        await c.client.post(base + "/review", headers=c.headers("a"), json=manual)
    ).status_code == 200
    view = (
        await c.client.get(f"/api/v1/assignments/{aid}/homework", headers=c.headers("a"))
    ).json()
    assert (
        next(row for row in view["students"] if row["participant_id"] == rows[0]["participant_id"])[
            "reviewed_count"
        ]
        == 0
    )


@pytest.mark.anyio
async def test_revoke_during_visual_scoring_blocks_commit_and_replay(homework, monkeypatch):
    import asyncio

    c = homework
    monkeypatch.setattr(visual_service.settings, "grading_enabled", True)
    monkeypatch.setattr(visual_service.settings, "grading_visual_enabled", True)
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider(ImageScoreProvider):
        async def complete_visual(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().complete_visual(**kwargs)

    monkeypatch.setattr(visual_service, "build_text_grading_provider", lambda _: WaitingProvider())
    _, qid, rows = await setup_assignment(c)
    sid = await upload(c, rows[0]["participant_id"])
    base = f"/api/v1/homework/submissions/{sid}/questions/{qid}/visual"
    selection = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        regions=[dict(page_index=0, x0=500, y0=500, x1=9000, y1=9000)],
    )
    assert (
        await c.client.post(base + "/selection", headers=c.headers("a"), json=selection)
    ).status_code == 200
    req = dict(request_id=str(uuid4()), expected_selection_revision=1)
    task = asyncio.create_task(c.client.post(base + "/score", headers=c.headers("a"), json=req))
    await asyncio.wait_for(entered.wait(), 10)
    async with c.factory() as db:
        teaching_id = await db.scalar(
            text(
                "SELECT id FROM homework_teaching_assignments "
                "WHERE teacher_id=CAST(:teacher AS uuid) "
                "AND class_id=CAST(:class AS uuid) AND revoked_at IS NULL"
            ),
            {"teacher": str(c.users["a"].id), "class": rows[0]["class_id"]},
        )
    revoked = await c.client.delete(
        f"/api/v1/homework/admin/teaching-assignments/{teaching_id}", headers=c.headers("admin")
    )
    assert revoked.status_code == 200, revoked.text
    release.set()
    result = await asyncio.wait_for(task, 10)
    assert result.status_code in (403, 404), result.text
    assert (await c.client.post(base + "/score", headers=c.headers("a"), json=req)).status_code in (
        403,
        404,
    )
    async with c.factory() as db:
        jobs = (
            await db.execute(text("SELECT status, proposal_id FROM homework_visual_jobs"))
        ).all()
        assert jobs == [("invalidated", None)]
