"""Signed HTTP, independent PostgreSQL/private S3; test scoring is visibly synthetic."""

# ruff: noqa: F401, F811
import json
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from test_exam_parsing import (
    auth_api,
    base,
    consume,
    exam,
    papers,
    parse_context,
    roster,
    scan_context,
    script_context,
    start,
)

from huipi_cloud.infrastructure.grading.provider import ProviderUsage, TextGradingResponse
from huipi_cloud.modules.exam_grading import service as g
from huipi_cloud.modules.exam_grading.permissions import MARK_OPERATOR_ROLE
from huipi_cloud.modules.exam_grading.runtime import ScoringWorker
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityScope


class SyntheticScoreProvider:
    provider_name = "ollama_native"
    model_id = "synthetic-test-only"
    generation_config_digest = "a" * 64

    async def complete(self, *, system_prompt, user_prompt):
        data = json.loads(user_prompt[user_prompt.index("{") :])
        # Use only the existing GradeInput prompt contract; never a production fallback.
        self.last_prompt = data
        segment = data["student_answer_segments"][0]["text"]
        reply = dict(
            schema_version="1.0",
            outcome="candidate",
            total_score=str(
                sum((Decimal(r["max_points"]) for r in data["rubric"]), Decimal("0.00"))
            ),
            criterion_scores=[
                dict(
                    criterion_id=r["criterion_id"],
                    awarded_points=r["max_points"],
                    rationale="Synthetic integration output; not teaching quality evidence",
                    evidence_references=[
                        dict(
                            kind="quote",
                            segment_index=0,
                            text_start=0,
                            text_end=len(segment),
                            quote=segment,
                        )
                    ],
                )
                for r in data["rubric"]
            ],
        )
        return TextGradingResponse(json.dumps(reply), self.model_id, 1, ProviderUsage(10, 10))


@pytest.fixture
async def marking(parse_context, request):
    c = parse_context
    async with c.factory() as db:
        await g.enable_operator(
            db,
            c.users["admin"].id,
            c.users["admin"].id,
            uuid4(),
            "synthetic exam marking deployment",
        )
    async with c.factory() as db:
        scope = await db.scalar(
            select(SecurityScope.id).where(SecurityScope.exam_session_id == UUID(c.session["id"]))
        )
    async with c.factory() as db:
        c.mark_grant = await security.grant_role(
            db,
            c.principals["admin"],
            uuid4(),
            user_id=c.users["a"].id,
            role_version_id=UUID(MARK_OPERATOR_ROLE),
            scope_id=scope,
        )
    assert (await start(c)).status_code == 202
    if (
        request.node.get_closest_marker("mineru_e2e")
        and __import__("os").getenv("MINERU_E2E") == "1"
    ):
        from huipi_cloud.core.config import Settings
        from huipi_cloud.infrastructure.parsing.mineru import build_mineru_executor
        from huipi_cloud.modules.exam_parsing.runtime import ProtectedExecutor

        config = Settings().model_copy(
            update={"max_upload_size_bytes": 32 * 1024**2, "mineru_max_pdf_pages": 100}
        )
        parser = build_mineru_executor(config)
        parser.storage = c.storage
        c.executor = ProtectedExecutor(parser, c.backend)
        c.worker.executor = c.executor
    await consume(c)
    async with c.factory() as db:
        c.jid = await db.scalar(select(g.parsing.J.c.id))
    response = await c.client.post(
        f"/api/v1/exam-parsing-jobs/{c.jid}/grading-workspace", json=dict(request_id=str(uuid4()))
    )
    assert response.status_code == 200, response.text
    c.wid = response.json()["workspace_ids"][0]
    c.grade_worker = ScoringWorker(c.factory, c.storage, SyntheticScoreProvider(), c.config)
    try:
        yield c
    finally:
        async with c.factory() as db, db.begin():
            await db.execute(
                text(
                    "TRUNCATE exam_grading_operations,exam_scoring_jobs,exam_grading_workspaces,"
                    "grade_review_revisions,grade_proposal_artifacts,grade_input_artifacts,"
                    "answer_review_decisions,answer_alignment_artifacts CASCADE"
                )
            )


async def detail(c):
    r = await c.client.get(f"/api/v1/exam-grading-workspaces/{c.wid}")
    assert r.status_code == 200, r.text
    return r.json()


async def confirm(c):
    d = await detail(c)
    node = d["nodes"][0]
    return await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/answer-review",
        json=dict(
            request_id=str(uuid4()),
            manual_attribution=True,
            expected_revision=d["answer_review"]["revision"] if d["answer_review"] else 0,
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


@pytest.mark.anyio
async def test_exam_mark_vertical_input_proposal_and_teacher_revision(marking):
    c = marking
    r = await confirm(c)
    assert r.status_code == 200, r.text
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
        json=dict(request_id=str(uuid4()), expected_answer_revision=1),
    )
    assert r.status_code == 202, r.text
    assert await c.grade_worker.once()
    d = await detail(c)
    assert d["scoring_status"] == "succeeded", d
    assert d["proposal"]["model_id"] == "synthetic-test-only"
    assert d["proposal"]["grading_status"] == "completed_candidate"
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/score-review",
        json=dict(
            request_id=str(uuid4()),
            expected_revision=0,
            payload=dict(
                schema_name="huipi.grading.review",
                schema_version="1.0",
                review_status="accepted",
                reviewer_ref="teacher_" + c.users["a"].id.hex,
            ),
        ),
    )
    assert r.status_code == 200, r.text
    d = await detail(c)
    assert d["history"][0]["review_status"] == "accepted"
    assert not any(key in json.dumps(d) for key in ["object_key", "access_key", "secret_key"])


@pytest.mark.anyio
async def test_no_human_confirmation_or_uncertain_never_produces_zero(marking):
    c = marking
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
        json=dict(request_id=str(uuid4()), expected_answer_revision=1),
    )
    assert r.status_code == 409
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/answer-review",
        json=dict(
            request_id=str(uuid4()),
            expected_revision=0,
            payload=dict(
                decision="uncertain",
                reason_codes=["source_quality_insufficient"],
                reviewer_ref="teacher_" + c.users["a"].id.hex,
            ),
        ),
    )
    assert r.status_code == 200, r.text
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
        json=dict(request_id=str(uuid4()), expected_answer_revision=1),
    )
    assert r.status_code == 409
    async with c.factory() as db:
        assert not (await db.execute(select(g.GP))).first()
        assert not (await db.execute(select(g.GI))).first()


@pytest.mark.anyio
async def test_requests_idempotent_revision_conflicts_and_revoked_replay(marking):
    import asyncio

    c = marking
    assert (await confirm(c)).status_code == 200
    body = dict(request_id=str(uuid4()), expected_answer_revision=1)
    a, b = await asyncio.gather(
        *[
            c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/scoring", json=body)
            for _ in range(2)
        ]
    )
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert await c.grade_worker.once()
    payload = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        payload=dict(
            schema_name="huipi.grading.review",
            schema_version="1.0",
            review_status="accepted",
            reviewer_ref="teacher_" + c.users["a"].id.hex,
        ),
    )
    a, b = await asyncio.gather(
        *[
            c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/score-review", json=payload)
            for _ in range(2)
        ]
    )
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    payload["request_id"] = str(uuid4())
    assert (
        await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/score-review", json=payload)
    ).status_code == 409
    async with c.factory() as db:
        await security.revoke_grant(
            db, c.principals["admin"], uuid4(), grant_id=UUID(c.mark_grant["grant_id"])
        )
    assert (
        await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/scoring", json=body)
    ).status_code == 404
    assert (await c.client.get(f"/api/v1/exam-grading-workspaces/{c.wid}")).status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["answer", "roster", "retire", "revoke", "deactivate"])
async def test_model_wait_source_changes_cannot_register_candidate(marking, change):
    import asyncio

    from sqlalchemy import update

    from huipi_cloud.modules.identity.models import User

    c = marking
    assert (await confirm(c)).status_code == 200
    assert (
        await c.client.post(
            f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
            json=dict(request_id=str(uuid4()), expected_answer_revision=1),
        )
    ).status_code == 202
    entered, release = asyncio.Event(), asyncio.Event()
    original = c.grade_worker.provider.complete

    async def blocked(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    c.grade_worker.provider.complete = blocked
    running = asyncio.create_task(c.grade_worker.once())
    await asyncio.wait_for(entered.wait(), 10)
    try:
        if change == "answer":
            assert (await confirm(c)).status_code == 200
        elif change == "roster":
            from test_exam_rosters import call, entries, freeze, req

            from huipi_cloud.modules.exam_rosters import service as rosters
            from huipi_cloud.modules.exam_rosters.schemas import RosterCorrection

            new = await call(
                c,
                rosters.correct_roster,
                RosterCorrection(
                    **req(c.frozen_roster).model_dump(),
                    reason="合成名单更正",
                    entries=entries(c, 4, unknown=True),
                ),
            )
            await freeze(c, new)
        elif change == "retire":
            from test_paper_versions import write

            from huipi_cloud.modules.papers.service import retire_paper

            async with c.factory() as db:
                await retire_paper(db, c.principals["admin"], write(c.paper))
        elif change == "revoke":
            async with c.factory() as db:
                await security.revoke_grant(
                    db, c.principals["admin"], uuid4(), grant_id=UUID(c.mark_grant["grant_id"])
                )
        else:
            async with c.factory() as db, db.begin():
                await db.execute(
                    update(User).where(User.id == c.users["a"].id).values(is_active=False)
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
async def test_plain_admin_cross_workspace_and_direct_sql_splice_rejected(marking):
    from sqlalchemy import insert, text
    from sqlalchemy.exc import DBAPIError

    c = marking
    from sqlalchemy import update

    from huipi_cloud.modules.identity.models import User

    async with c.factory() as db, db.begin():
        await db.execute(update(User).where(User.id == c.users["b"].id).values(is_admin=True))
    for actor in ["b", "reader"]:
        c.client.headers.update(c.headers(actor))
        r = await c.client.get(f"/api/v1/exam-grading-workspaces/{c.wid}")
        assert r.status_code == 404 and "Synthetic executor" not in r.text
    c.client.headers.update(c.headers("a"))
    async with c.factory() as db:
        w = await g.row(db, g.W, UUID(c.wid))
        other = await db.scalar(
            select(g.QM.c.id).where(g.QM.c.legacy_question_id != w["question_id"])
        )
    with pytest.raises(DBAPIError):
        async with c.factory() as db, db.begin():
            await db.execute(
                insert(g.W).values(**{**dict(w), "id": uuid4(), "paper_question_mapping_id": other})
            )
    with pytest.raises(DBAPIError):
        async with c.factory() as db, db.begin():
            await db.execute(text("DELETE FROM exam_grading_workspaces"))


@pytest.mark.anyio
async def test_legacy_grading_cannot_consume_exam_input(marking):
    from huipi_cloud.modules.answer_alignment.errors import AnswerAlignmentNotFoundError
    from huipi_cloud.modules.answer_alignment.service import align_submission
    from huipi_cloud.modules.grading import service as legacy_input
    from huipi_cloud.modules.grading.errors import GradeInputNotFoundError

    c = marking
    assert (await confirm(c)).status_code == 200
    assert (
        await c.client.post(
            f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
            json=dict(request_id=str(uuid4()), expected_answer_revision=1),
        )
    ).status_code == 202
    async with c.factory() as db:
        iid = await db.scalar(select(g.GI.c.id))
        sid = await db.scalar(select(g.GI.c.submission_id))
    with pytest.raises(GradeInputNotFoundError):
        await legacy_input.read_grade_input_artifact(c.factory, c.storage, iid, config=c.config)
    with pytest.raises(AnswerAlignmentNotFoundError):
        await align_submission(c.factory, c.storage, sid, config=c.config)


@pytest.mark.anyio
@pytest.mark.mineru_e2e
@pytest.mark.skipif(
    __import__("os").getenv("RUN_EXAM_OLLAMA") != "1",
    reason="explicit cached MinerU/local Ollama only",
)
async def test_cached_mineru_and_real_local_ollama_grading(marking, tmp_path):
    from huipi_cloud.core.config import Settings
    from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider

    c = marking
    d = await detail(c)
    node = next(n for n in d["nodes"] if "2 + 2 = 4" in n["text"] or "2+2=4" in n["text"])
    expression = "2 + 2 = 4" if "2 + 2 = 4" in node["text"] else "2+2=4"
    offset = node["text"].index(expression)
    body = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        manual_attribution=True,
        payload=dict(
            decision="response_present",
            response_regions=[
                dict(
                    content_pointer=node["content_pointer"],
                    text_start=offset,
                    text_end=offset + len(expression),
                )
            ],
            reason_codes=["student_work_visible"],
            reviewer_ref="teacher_" + c.users["a"].id.hex,
        ),
    )
    r = await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/answer-review", json=body)
    assert r.status_code == 200, r.text
    config = Settings(
        _env_file=None,
        grading_enabled=True,
        grading_provider="ollama_native",
        grading_base_url="http://127.0.0.1:11434",
        grading_model="qwen3.5:9b-q4_K_M",
        grading_max_output_tokens=4096,
        grading_timeout_seconds=120,
        grading_invalid_response_retries=0,
    )
    from pathlib import Path

    evidence_dir = Path(__import__("os").getenv("EXAM_GRADING_EVIDENCE_DIR", str(tmp_path)))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    provider = build_text_grading_provider(config)
    original = provider.complete

    async def record(**kwargs):
        response = await original(**kwargs)
        from pathlib import Path

        (evidence_dir / "local-provider-response.json").write_text(
            json.dumps(
                dict(
                    model_id=response.model_id,
                    content=response.content,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    elapsed_ms=response.elapsed_ms,
                ),
                ensure_ascii=False,
            )
        )
        return response

    provider.complete = record
    worker = ScoringWorker(c.factory, c.storage, provider, config)
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
        json=dict(request_id=str(uuid4()), expected_answer_revision=1),
    )
    assert r.status_code == 202, r.text
    assert await worker.once()
    d = await detail(c)
    assert d["scoring_status"] == "succeeded", d["failure_code"]
    assert d["proposal"]["model_id"] == "qwen3.5:9b-q4_K_M"
    assert d["proposal"]["grading_status"] == "completed_candidate"
    from pathlib import Path

    (evidence_dir / "local-model-result.json").write_text(
        json.dumps(
            {
                k: d["proposal"][k]
                for k in [
                    "model_id",
                    "grading_status",
                    "total_score",
                    "prompt_tokens",
                    "completion_tokens",
                    "model_elapsed_ms",
                    "prompt_version",
                ]
            },
            ensure_ascii=False,
        )
    )


def grading_mixed_scan_pdf():
    """Synthetic mixed A2/B1/A1/B2 pages consistent with the frozen test Paper's 2+2."""
    from io import BytesIO

    from PIL import Image, ImageDraw

    images = []
    for number, klass, sheet in [("N001", 1, 2), ("N002", 2, 1), ("N001", 1, 1), ("N002", 2, 2)]:
        image = Image.new("RGB", (612, 792), "white")
        draw = ImageDraw.Draw(image)
        for y, text_value in [
            (55, "Synthetic exam - no QR code"),
            (95, f"Name: Same Name / Class: {klass}"),
            (125, f"Exam number: {number} / Sheet: {sheet} of 2"),
            (175, f"Question {sheet}: calculate 2+2."),
            (215, "Student response: 2+2=4."),
        ]:
            draw.text((45, y), text_value, fill="black")
        images.append(image)
    result = BytesIO()
    try:
        images[0].save(result, format="PDF", save_all=True, append_images=images[1:])
        return result.getvalue()
    finally:
        for image in images:
            image.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kind", ["positive_without_quote", "total_mismatch", "out_of_range", "abstain", "timeout"]
)
async def test_strict_score_failures_and_abstention_are_not_zero(marking, kind):
    from huipi_cloud.modules.grading.proposal_errors import GradeProposalProviderTimeoutError

    c = marking
    assert (await confirm(c)).status_code == 200
    assert (
        await c.client.post(
            f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
            json=dict(request_id=str(uuid4()), expected_answer_revision=1),
        )
    ).status_code == 202
    original = c.grade_worker.provider.complete

    async def altered(**kwargs):
        if kind == "timeout":
            raise GradeProposalProviderTimeoutError("provider_timeout")
        response = await original(**kwargs)
        body = json.loads(response.content)
        if kind == "abstain":
            body = dict(
                schema_version="1.0",
                outcome="abstain",
                abstention_reason="Synthetic unclear material",
                reason_codes=["insufficient_evidence"],
            )
        elif kind == "positive_without_quote":
            body["criterion_scores"][0]["evidence_references"] = [
                dict(kind="no_evidence", reason="Missing step")
            ]
        elif kind == "total_mismatch":
            body["total_score"] = "0.00"
        else:
            body["criterion_scores"][0]["awarded_points"] = "999.00"
        return TextGradingResponse(json.dumps(body), "synthetic-test-only", 1, ProviderUsage(1, 1))

    c.grade_worker.provider.complete = altered
    assert await c.grade_worker.once()
    d = await detail(c)
    if kind == "abstain":
        assert d["scoring_status"] == "succeeded"
        assert (
            d["proposal"]["grading_status"] == "grading_abstained"
            and d["proposal"]["total_score"] is None
        )
    else:
        assert d["scoring_status"] == "failed" and d["proposal"] is None
        async with c.factory() as db:
            assert not (await db.execute(select(g.GP))).first()


@pytest.mark.anyio
async def test_candidate_audit_failure_is_atomic(marking):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    c = marking
    assert (await confirm(c)).status_code == 200
    assert (
        await c.client.post(
            f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
            json=dict(request_id=str(uuid4()), expected_answer_revision=1),
        )
    ).status_code == 202
    engine = c.factory.kw["bind"].sync_engine

    def fail(conn, cursor, statement, params, ctx, many):
        if statement.startswith("INSERT INTO exam_grading_operations") and "candidate" in str(
            params
        ):
            raise OperationalError("synthetic", {}, Exception("audit failed"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(OperationalError):
            await c.grade_worker.once()
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    async with c.factory() as db:
        assert not (await db.execute(select(g.GP))).first()
        assert await db.scalar(select(g.S.c.status)) == "running"


@pytest.mark.anyio
async def test_teacher_revised_complete_snapshots_and_immutable_history(marking):
    from sqlalchemy.exc import DBAPIError

    c = marking
    assert (await confirm(c)).status_code == 200
    assert (
        await c.client.post(
            f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
            json=dict(request_id=str(uuid4()), expected_answer_revision=1),
        )
    ).status_code == 202
    assert await c.grade_worker.once()
    d = await detail(c)
    scores = d["proposal"]["criterion_scores"]
    assert len(scores) == 2
    for expected, points in [(0, ["3.00", "5.00"]), (1, ["3.00", "4.00"])]:
        items = [
            dict(
                criterion_id=s["criterion_id"],
                reviewed_points=value,
                **(
                    dict(
                        rationale="Synthetic teacher revision",
                        evidence_references=s["evidence_references"],
                    )
                    if Decimal(value) != Decimal(s["awarded_points"])
                    else {}
                ),
            )
            for s, value in zip(scores, points, strict=True)
        ]
        r = await c.client.post(
            f"/api/v1/exam-grading-workspaces/{c.wid}/score-review",
            json=dict(
                request_id=str(uuid4()),
                expected_revision=expected,
                payload=dict(
                    schema_name="huipi.grading.review",
                    schema_version="1.0",
                    review_status="revised",
                    reviewer_ref="teacher_" + c.users["a"].id.hex,
                    criterion_reviews=items,
                ),
            ),
        )
        assert r.status_code == 200, r.text
    d = await detail(c)
    a, b = d["history"]
    assert b["supersedes_review_id"] == a["review_id"]
    assert [i["reviewed_points"] for i in a["criterion_reviews"]] == ["3.00", "5.00"]
    assert [i["reviewed_points"] for i in b["criterion_reviews"]] == ["3.00", "4.00"]
    assert all(i["proposed_points"] == "5.00" for h in d["history"] for i in h["criterion_reviews"])
    with pytest.raises(DBAPIError):
        async with c.factory() as db, db.begin():
            await db.execute(text("UPDATE grade_review_revisions SET reviewed_total_score=0"))


@pytest.mark.anyio
async def test_lost_teacher_commit_confirmation_replays_one_revision(marking):
    from sqlalchemy import event, func
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    c = marking
    assert (await confirm(c)).status_code == 200
    assert (
        await c.client.post(
            f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
            json=dict(request_id=str(uuid4()), expected_answer_revision=1),
        )
    ).status_code == 202
    assert await c.grade_worker.once()
    state = {"written": False}

    def written(conn, cursor, stmt, params, ctx, many):
        if stmt.startswith("INSERT INTO grade_review_revisions"):
            state["written"] = True

    def lost(session):
        if state["written"]:
            state["written"] = False
            raise OperationalError("synthetic", {}, Exception("COMMIT acknowledgement lost"))

    engine = c.factory.kw["bind"].sync_engine
    body = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        payload=dict(
            schema_name="huipi.grading.review",
            schema_version="1.0",
            review_status="accepted",
            reviewer_ref="teacher_" + c.users["a"].id.hex,
        ),
    )
    event.listen(engine, "before_cursor_execute", written)
    event.listen(Session, "after_commit", lost)
    try:
        r = await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/score-review", json=body)
    finally:
        event.remove(engine, "before_cursor_execute", written)
        event.remove(Session, "after_commit", lost)
    assert r.status_code == 503
    r = await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/score-review", json=body)
    assert r.status_code == 200, r.text
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(g.GR)) == 1


@pytest.mark.anyio
async def test_competing_workers_and_expired_lease_cannot_register_candidate(marking):
    import asyncio

    from sqlalchemy import func, update

    from huipi_cloud.modules.exam_parsing.runtime import worker_transaction

    c = marking
    assert (await confirm(c)).status_code == 200
    assert (
        await c.client.post(
            f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
            json=dict(request_id=str(uuid4()), expected_answer_revision=1),
        )
    ).status_code == 202
    claims = await asyncio.gather(c.grade_worker.claim(), c.grade_worker.claim())
    assert sum(j is not None for j in claims) == 1
    job = next(j for j in claims if j is not None)
    async with worker_transaction(c.factory) as db:
        assert await c.grade_worker.leased(db, job["id"], uuid4()) is None
        await db.execute(
            update(g.S)
            .where(g.S.c.id == job["id"])
            .values(lease_expires_at=func.clock_timestamp() - text("INTERVAL '1 second'"))
        )
    # The model may return, but its stale task cannot commit a late proposal/index.
    assert await c.grade_worker.generate(job) is False
    assert await c.grade_worker.claim() is None
    d = await detail(c)
    assert d["scoring_status"] == "failed"
    assert d["failure_code"] == "execution_outcome_unknown"
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(g.GP)) == 0


@pytest.mark.anyio
async def test_lost_candidate_commit_ack_recovers_actual_success_without_model_retry(marking):
    from sqlalchemy import event, func
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    c = marking
    assert (await confirm(c)).status_code == 200
    body = dict(request_id=str(uuid4()), expected_answer_revision=1)
    r = await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/scoring", json=body)
    assert r.status_code == 202
    jid = r.json()["scoring_job_id"]
    state = {"written": False}

    def written(conn, cursor, stmt, params, ctx, many):
        if stmt.startswith("INSERT INTO grade_proposal_artifacts"):
            state["written"] = True

    def lost(session):
        if state["written"]:
            state["written"] = False
            raise OperationalError("synthetic", {}, Exception("COMMIT acknowledgement lost"))

    engine = c.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", written)
    event.listen(Session, "after_commit", lost)
    try:
        with pytest.raises(OperationalError):
            await c.grade_worker.once()
    finally:
        event.remove(engine, "before_cursor_execute", written)
        event.remove(Session, "after_commit", lost)
    d = await detail(c)
    assert d["scoring_status"] == "succeeded"
    assert d["proposal"]["model_id"] == "synthetic-test-only"
    assert not await c.grade_worker.once()
    replay = await c.client.post(f"/api/v1/exam-grading-workspaces/{c.wid}/scoring", json=body)
    assert replay.status_code == 202 and replay.json()["scoring_job_id"] == jid
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(g.GP)) == 1


@pytest.mark.anyio
async def test_grading_facts_prevent_destructive_migration_downgrade(marking):
    import asyncio
    import os
    import sys

    c = marking
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "downgrade",
        "b14b41a2e903",
        env=dict(os.environ, DATABASE_URL=os.environ["TEST_DATABASE_URL"]),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode != 0 and b"exam_mark_downgrade_requires_archived_data" in output
    async with c.factory() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "b14c52b3f014"
        assert await db.scalar(select(g.W.c.id).where(g.W.c.id == UUID(c.wid))) == UUID(c.wid)


@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["input", "proposal"])
async def test_storage_failure_never_leaves_success_index(marking, monkeypatch, stage):
    from sqlalchemy import func

    from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError

    c = marking
    assert (await confirm(c)).status_code == 200
    original = c.storage.upload_fileobj

    async def unavailable(fileobj, object_key, content_type):
        if f"/exam-mark/{stage}/" in object_key:
            raise StorageUnavailableError("synthetic private storage unavailable")
        return await original(fileobj, object_key, content_type)

    monkeypatch.setattr(c.storage, "upload_fileobj", unavailable)
    r = await c.client.post(
        f"/api/v1/exam-grading-workspaces/{c.wid}/scoring",
        json=dict(request_id=str(uuid4()), expected_answer_revision=1),
    )
    assert r.status_code == (503 if stage == "input" else 202), r.text
    if stage == "proposal":
        assert await c.grade_worker.once()
        assert (await detail(c))["scoring_status"] == "failed"
    async with c.factory() as db:
        assert await db.scalar(select(func.count()).select_from(g.GP)) == 0
        assert await db.scalar(select(func.count()).select_from(g.GR)) == 0
        assert await db.scalar(select(func.count()).select_from(g.GI)) == (stage == "proposal")
        assert await db.scalar(select(func.count()).select_from(g.S)) == (stage == "proposal")
