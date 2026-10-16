"""Signed HTTP + actual independent PostgreSQL/private S3; synthetic source and Fake model."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.exc import DBAPIError, OperationalError
from test_grade_review import _make_candidate, _review_payload
from test_teacher_authorization import auth_api  # noqa: F401

from huipi_cloud.modules.assignments.models import AnswerKey, Question
from huipi_cloud.modules.grading import grade_review_repository as repository
from huipi_cloud.modules.grading import grade_review_service as service
from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision
from huipi_cloud.modules.grading.models import GradeInputArtifact, GradeProposalArtifact
from huipi_cloud.modules.identity.models import AssignmentMembership, User


@pytest.fixture
async def grade_http(auth_api, postgres_engine):  # noqa: F811
    ctx = auth_api
    old = ctx.client.headers.copy()
    ctx.client.headers.update(ctx.headers())
    try:
        (
            ctx.factory,
            ctx.sid,
            ctx.question,
            ctx.answer_review,
            ctx.prepared,
            ctx.grade_input,
            ctx.proposal,
            ctx.sha,
            _,
            ctx.provider,
        ) = await _make_candidate(
            ctx.client,
            ctx.storage,
            postgres_engine,
            rubric_items=[("正确计算", "5.00"), ("正确结果", "5.00")],
        )
        ctx.aid = ctx.grade_input.assignment_id
        ctx.base = (
            f"/api/v1/submissions/{ctx.sid}/questions/{ctx.question.id}"
            f"/grade-proposals/{ctx.proposal.proposal_id}"
        )
        yield ctx
    finally:
        ctx.client.headers.clear()
        ctx.client.headers.update(old)
        async with ctx.factory() as session, session.begin():
            await session.execute(text("TRUNCATE assignments CASCADE"))


def payload(status="accepted", revision=0):
    result = {"request_id": str(uuid4()), "expected_revision": revision, "review_status": status}
    if status in {"rejected", "regrading_required"}:
        result["review_reason_codes"] = ["candidate_not_reliable"]
    return result


def revised(ctx, scores, revision):
    result = payload("revised", revision)
    result["criterion_reviews"] = []
    for item, points in zip(ctx.proposal.criterion_scores, scores, strict=True):
        row = {"criterion_id": str(item.criterion_id), "reviewed_points": points}
        if str(item.awarded_points) != points:
            row.update(
                rationale="合成人工复核依据",
                evidence_references=[
                    ref.model_dump(mode="json") for ref in item.evidence_references
                ],
            )
        result["criterion_reviews"].append(row)
    return result


async def records(ctx):
    async with ctx.factory() as session:
        return list(
            (
                await session.scalars(
                    select(GradeReviewRevision).order_by(GradeReviewRevision.review_revision)
                )
            ).all()
        )


async def grant(ctx, name, write=True):
    response = await ctx.client.put(
        f"/api/v1/assignments/{ctx.aid}/members/{ctx.users[name].id}",
        headers=ctx.headers("admin"),
        json={"can_write": write},
    )
    assert response.status_code == 200, response.text


@pytest.mark.anyio
async def test_signed_review_full_snapshots_history_and_original_sources_unchanged(grade_http):
    ctx = grade_http
    keys = set(ctx.storage.uploaded_keys)
    async with ctx.factory() as session:
        input_before = await session.scalar(select(GradeInputArtifact.object_sha256))
        proposal_before = await session.scalar(select(GradeProposalArtifact.object_sha256))
    view = await ctx.client.get(ctx.base + "/review")
    assert view.status_code == 200, view.text
    assert view.json()["source_status"] == "current"
    assert view.json()["grading_status"] == "completed_candidate"
    assert not view.json()["grade_published"] and not view.json()["has_review_history"]
    first = await ctx.client.post(ctx.base + "/reviews", json=payload())
    assert first.status_code == 201, first.text
    one = first.json()["record"]
    assert one["reviewed_total_score"] == "10.00"
    assert one["reviewer_user_id"] == str(ctx.users["a"].id)
    assert one["reviewer_ref"] == f"teacher_{ctx.users['a'].id.hex}"
    assert one["reviewer_kind"] == "authenticated_teacher"
    for scores, revision in [(["3.00", "5.00"], 1), (["3.00", "4.00"], 2)]:
        response = await ctx.client.post(ctx.base + "/reviews", json=revised(ctx, scores, revision))
        assert response.status_code == 201, response.text
        assert [
            x["reviewed_points"] for x in response.json()["record"]["criterion_scores"]
        ] == scores
    history = (await ctx.client.get(ctx.base + "/reviews?limit=2")).json()
    assert history["has_next"] and history["next_offset"] == 2
    three, two = history["records"]
    assert three["supersedes_review_id"] == two["review_id"]
    assert two["supersedes_review_id"] == one["review_id"]
    assert [x["proposed_points"] for x in three["criterion_scores"]] == ["5.00", "5.00"]
    assert [x["changed"] for x in three["criterion_scores"]] == [True, True]
    assert [x["changed"] for x in two["criterion_scores"]] == [True, False]
    assert (await ctx.client.get(ctx.base + "/reviews?offset=2")).json()["records"][0] == one
    # Accepted after revisions still accepts the ORIGINAL model candidate.
    result = await ctx.client.post(ctx.base + "/reviews", json=payload(revision=3))
    assert result.json()["record"]["reviewed_total_score"] == "10.00"
    for status, revision in [("rejected", 4), ("regrading_required", 5)]:
        result = await ctx.client.post(ctx.base + "/reviews", json=payload(status, revision))
        assert result.status_code == 201, result.text
        assert result.json()["record"]["reviewed_total_score"] is None
        assert result.json()["record"]["criterion_scores"] == []
        assert not result.json()["grade_published"]
    view = await ctx.client.get(ctx.base + "/review")
    assert view.json()["regrading_required"] and view.json()["latest_review_revision"] == 6
    assert view.json()["reviewer_identity_authenticated"]
    assert ctx.provider.calls == 1 and set(ctx.storage.uploaded_keys) == keys
    async with ctx.factory() as session:
        assert await session.scalar(select(GradeInputArtifact.object_sha256)) == input_before
        assert await session.scalar(select(GradeProposalArtifact.object_sha256)) == proposal_before
    assert len(await records(ctx)) == 6
    for response in [view, await ctx.client.get(ctx.base + "/reviews")]:
        assert response.headers["cache-control"] == "private, no-store"
        for secret in (
            ctx.grade_input.selected_answer_segments[0].text,
            ctx.proposal.criterion_scores[0].rationale,
            "object_key",
            "合成人工复核依据",
        ):
            assert secret not in response.text


@pytest.mark.anyio
async def test_read_write_scope_revocation_and_authorization_before_storage(
    grade_http, monkeypatch
):
    ctx = grade_http
    await grant(ctx, "reader", False)
    body = payload()
    assert (
        await ctx.client.get(ctx.base + "/review", headers=ctx.headers("reader"))
    ).status_code == 200
    assert (
        await ctx.client.post(ctx.base + "/reviews", json=body, headers=ctx.headers("reader"))
    ).status_code == 404
    assert (await ctx.client.post(ctx.base + "/reviews", json=body)).status_code == 201
    await ctx.client.delete(
        f"/api/v1/assignments/{ctx.aid}/members/{ctx.users['a'].id}", headers=ctx.headers("admin")
    )
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Unauthorized request reached S3")

    monkeypatch.setattr(ctx.storage, "download", forbidden)
    for who in ("a", "b"):
        for suffix in ("/review", "/reviews"):
            response = await ctx.client.get(ctx.base + suffix, headers=ctx.headers(who))
            assert response.status_code == 404
        assert (
            await ctx.client.post(ctx.base + "/reviews", json=body, headers=ctx.headers(who))
        ).status_code == 404
    assert not calls and len(await records(ctx)) == 1


@pytest.mark.anyio
async def test_admin_cannot_splice_resources_even_within_same_assignment(grade_http):
    ctx = grade_http
    async with ctx.factory() as session, session.begin():
        other = Question(
            assignment_id=ctx.aid,
            question_number=2,
            question_type="short_answer",
            stem="合成另一题",
            max_score="10",
        )
        session.add(other)
        await session.flush()
        qid = other.id
    paths = [
        ctx.base.replace(str(ctx.question.id), str(qid)),
        ctx.base.replace(str(ctx.sid), str(uuid4())),
        ctx.base.replace(str(ctx.proposal.proposal_id), str(uuid4())),
    ]
    for base in paths:
        assert (
            await ctx.client.get(base + "/review", headers=ctx.headers("admin"))
        ).status_code == 404
        assert (
            await ctx.client.post(base + "/reviews", headers=ctx.headers("admin"), json=payload())
        ).status_code == 404
    assert not await records(ctx)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field",
    ["reviewer_ref", "reviewer_user_id", "reviewer_id", "teacher_id", "role", "assignment_id"],
)
async def test_http_rejects_all_client_identity_claims(grade_http, field):
    ctx = grade_http
    body = payload()
    body[field] = str(uuid4())
    assert (await ctx.client.post(ctx.base + "/reviews", json=body)).status_code == 422
    assert not await records(ctx)


@pytest.mark.anyio
async def test_replay_body_and_actor_binding_and_stale_revision(grade_http):
    ctx = grade_http
    await grant(ctx, "b")
    body = payload()
    first = await ctx.client.post(ctx.base + "/reviews", json=body)
    replay = await ctx.client.post(ctx.base + "/reviews", json=body)
    assert first.status_code == 201 and replay.status_code == 200
    assert replay.json()["record"] == first.json()["record"] and not replay.json()["created"]
    changed = {**body, "review_comment": "不同内容"}
    for data, who in [(changed, "a"), (body, "b"), (payload(), "a")]:
        assert (
            await ctx.client.post(ctx.base + "/reviews", json=data, headers=ctx.headers(who))
        ).status_code == 409
    assert len(await records(ctx)) == 1


@pytest.mark.anyio
async def test_two_teachers_same_revision_only_one_append(grade_http, monkeypatch):
    ctx = grade_http
    await grant(ctx, "b")
    barrier = asyncio.Barrier(2)
    original = repository.append_review

    async def synchronized(*args, **kwargs):
        await asyncio.wait_for(barrier.wait(), timeout=10)
        return await original(*args, **kwargs)

    monkeypatch.setattr(repository, "append_review", synchronized)
    results = await asyncio.gather(
        ctx.client.post(ctx.base + "/reviews", json=payload()),
        ctx.client.post(ctx.base + "/reviews", headers=ctx.headers("b"), json=payload()),
    )
    assert sorted(x.status_code for x in results) == [201, 409]
    rows = await records(ctx)
    assert len(rows) == 1 and rows[0].review_revision == 1
    assert rows[0].reviewer_user_id in {ctx.users["a"].id, ctx.users["b"].id}


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["answer_key", "proposal", "input"])
async def test_changed_source_blocks_new_review_but_authorized_replay_remains(grade_http, change):
    ctx = grade_http
    body = payload()
    assert (await ctx.client.post(ctx.base + "/reviews", json=body)).status_code == 201
    async with ctx.factory() as session, session.begin():
        if change == "answer_key":
            stmt = (
                update(AnswerKey)
                .where(AnswerKey.question_id == ctx.question.id)
                .values(answer_content="5")
            )
        elif change == "proposal":
            stmt = (
                update(GradeProposalArtifact)
                .where(GradeProposalArtifact.id == ctx.proposal.proposal_id)
                .values(object_sha256="0" * 64)
            )
        else:
            stmt = (
                update(GradeInputArtifact)
                .where(GradeInputArtifact.id == ctx.prepared.artifact_id)
                .values(object_sha256="0" * 64)
            )
        await session.execute(stmt)
    assert (await ctx.client.post(ctx.base + "/reviews", json=body)).status_code == 200
    response = await ctx.client.post(ctx.base + "/reviews", json=payload(revision=1))
    assert response.status_code in {409, 503}
    assert len(await records(ctx)) == 1


@pytest.mark.anyio
async def test_revocation_and_user_disable_between_s3_and_commit(grade_http, monkeypatch):
    ctx = grade_http
    original = repository.append_review

    async def revoke_then_append(*args, **kwargs):
        async with ctx.factory() as session, session.begin():
            await session.execute(
                update(AssignmentMembership)
                .where(
                    AssignmentMembership.assignment_id == ctx.aid,
                    AssignmentMembership.user_id == ctx.users["a"].id,
                )
                .values(revoked_at=text("CURRENT_TIMESTAMP"))
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(repository, "append_review", revoke_then_append)
    assert (await ctx.client.post(ctx.base + "/reviews", json=payload())).status_code == 404
    assert not await records(ctx)
    await grant(ctx, "a")

    async def disable_then_append(*args, **kwargs):
        async with ctx.factory() as session, session.begin():
            await session.execute(
                update(User).where(User.id == ctx.users["a"].id).values(is_active=False)
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(repository, "append_review", disable_then_append)
    assert (await ctx.client.post(ctx.base + "/reviews", json=payload())).status_code == 403
    assert not await records(ctx)


@pytest.mark.anyio
async def test_short_permission_share_locks_block_concurrent_revocation(grade_http, monkeypatch):
    ctx = grade_http
    original = repository.require_assignment
    reached, release = asyncio.Event(), asyncio.Event()

    async def locked(*args, **kwargs):
        await original(*args, **kwargs)
        if kwargs.get("lock"):
            reached.set()
            await asyncio.wait_for(release.wait(), timeout=10)

    monkeypatch.setattr(repository, "require_assignment", locked)
    request = asyncio.create_task(ctx.client.post(ctx.base + "/reviews", json=payload()))
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        async with ctx.factory() as session:
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(text("SET LOCAL lock_timeout = '50ms'"))
                    await session.execute(
                        update(AssignmentMembership)
                        .where(
                            AssignmentMembership.assignment_id == ctx.aid,
                            AssignmentMembership.user_id == ctx.users["a"].id,
                        )
                        .values(can_write=False)
                    )
    finally:
        release.set()
    assert (await request).status_code == 201
    assert len(await records(ctx)) == 1


@pytest.mark.anyio
async def test_insert_rollback_lost_commit_ack_and_safe_replay(
    grade_http, monkeypatch, postgres_engine
):
    ctx = grade_http
    body = payload()

    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO GRADE_REVIEW_REVISIONS"):
            raise OperationalError("redacted", {}, RuntimeError("synthetic"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail_insert)
    try:
        result = await ctx.client.post(ctx.base + "/reviews", json=body)
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail_insert)
    assert result.status_code == 503 and not await records(ctx)
    original = repository.append_review

    async def lost_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise OperationalError("redacted", {}, RuntimeError("synthetic lost ack"))

    monkeypatch.setattr(repository, "append_review", lost_ack)
    assert (await ctx.client.post(ctx.base + "/reviews", json=body)).status_code == 503
    assert len(await records(ctx)) == 1
    monkeypatch.setattr(repository, "append_review", original)
    assert (await ctx.client.post(ctx.base + "/reviews", json=body)).status_code == 200
    assert len(await records(ctx)) == 1


@pytest.mark.anyio
async def test_legacy_identity_and_pg_update_delete_protection(grade_http):
    ctx = grade_http
    legacy = await service.record_grade_review(
        ctx.factory,
        ctx.storage,
        proposal_id=ctx.proposal.proposal_id,
        request_id=uuid4(),
        expected_revision=0,
        payload=_review_payload(reviewer_ref=f"teacher_{ctx.users['a'].id.hex}"),
    )
    summary = (await ctx.client.get(ctx.base + "/reviews")).json()["records"][0]
    assert summary["reviewer_kind"] == "legacy_cli_claim" and summary["reviewer_user_id"] is None
    for stmt in [
        update(GradeReviewRevision).values(reviewer_ref="tampered"),
        delete(GradeReviewRevision),
    ]:
        async with ctx.factory() as session:
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(stmt)
    rows = await records(ctx)
    assert len(rows) == 1 and rows[0].review_id == legacy.review.review_id


@pytest.mark.anyio
@pytest.mark.parametrize(
    "bad", ["unknown", "missing", "too_high", "float", "no_change", "no_quote", "forged_quote"]
)
async def test_revised_keeps_decimal_rubric_and_quote_gates(grade_http, bad):
    ctx = grade_http
    body = revised(ctx, ["3.00", "4.00"], 0)
    row = body["criterion_reviews"][0]
    if bad == "unknown":
        row["criterion_id"] = str(uuid4())
    elif bad == "missing":
        body["criterion_reviews"].pop()
    elif bad == "too_high":
        row["reviewed_points"] = "5.01"
    elif bad == "float":
        row["reviewed_points"] = 3.0
    elif bad == "no_change":
        body = revised(ctx, ["5.00", "5.00"], 0)
    elif bad == "no_quote":
        row["evidence_references"] = [{"kind": "no_evidence", "reason": "未见相关步骤"}]
    else:
        row["evidence_references"][0]["quote"] = "伪造原文"
    result = await ctx.client.post(ctx.base + "/reviews", json=body)
    assert result.status_code == 422, result.text
    assert not await records(ctx)


@pytest.mark.anyio
async def test_pagination_limits_and_storage_failure_safe_response(grade_http, monkeypatch):
    from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError

    ctx = grade_http
    assert (await ctx.client.get(ctx.base + "/reviews?limit=11")).status_code == 422
    assert (await ctx.client.get(ctx.base + "/reviews?offset=-1")).status_code == 422

    async def fail(*args, **kwargs):
        raise StorageUnavailableError("synthetic-private-path-and-key")

    monkeypatch.setattr(ctx.storage, "download", fail)
    for method, suffix in [("get", "/review"), ("post", "/reviews")]:
        result = await getattr(ctx.client, method)(
            ctx.base + suffix, **({"json": payload()} if method == "post" else {})
        )
        assert result.status_code == 503
        assert "synthetic-private-path" not in result.text
    assert not await records(ctx)


@pytest.mark.anyio
async def test_grade_review_migration_roundtrip_keeps_legacy_snapshot_and_immutability(
    client,
    postgres_engine,
    recording_minio_storage,
):
    import os
    import subprocess
    import sys

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from huipi_cloud.infrastructure.database.session import get_db_session
    from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
    from huipi_cloud.main import app
    from huipi_cloud.modules.identity.dependencies import authorize_request

    url = postgres_engine.url
    name = f"huipi_grade_review_migration_{uuid4().hex}_test"
    target = url.set(database=name)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine = create_async_engine(target)
    environment = dict(os.environ, DATABASE_URL=target.render_as_string(hide_password=False))
    previous = app.dependency_overrides.copy()
    created = False

    async def migrate(*args):
        process = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", *args],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert process.returncode == 0, process.stderr[-3000:]

    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        await migrate("upgrade", "head")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        principal = app.dependency_overrides[authorize_request]()
        async with factory() as session, session.begin():
            session.add(
                User(
                    id=principal.user_id,
                    issuer=principal.issuer,
                    subject=principal.subject,
                    is_admin=True,
                    is_active=True,
                )
            )

        async def scratch():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db_session] = scratch
        app.dependency_overrides[get_object_storage] = lambda: recording_minio_storage
        _, _, _, _, _, _, proposal, _, _, _ = await _make_candidate(
            client,
            recording_minio_storage,
            engine,
        )
        legacy = await service.record_grade_review(
            factory,
            recording_minio_storage,
            proposal_id=proposal.proposal_id,
            request_id=uuid4(),
            expected_revision=0,
            payload=_review_payload(),
        )
        rid = legacy.review.review_id
        async with engine.connect() as connection:
            before = await connection.scalar(
                text(
                    "SELECT to_jsonb(r) - 'reviewer_user_id' "
                    "FROM grade_review_revisions r WHERE review_id=:id"
                ),
                {"id": rid},
            )
        await engine.dispose()
        await migrate("downgrade", "9d2a17c4e6b0")
        async with engine.connect() as connection:
            after = await connection.scalar(
                text("SELECT to_jsonb(r) FROM grade_review_revisions r WHERE review_id=:id"),
                {"id": rid},
            )
        assert before == after
        await engine.dispose()
        await migrate("upgrade", "head")
        await migrate("check")
        async with factory() as session:
            restored = await session.get(GradeReviewRevision, rid)
            assert restored.reviewer_user_id is None
            assert service._row_to_document(restored) == legacy.review
        for stmt in [
            update(GradeReviewRevision).values(reviewer_ref="tampered"),
            delete(GradeReviewRevision),
        ]:
            async with factory() as session:
                with pytest.raises(DBAPIError):
                    async with session.begin():
                        await session.execute(stmt)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()
        if created:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.anyio
async def test_same_request_concurrency_creates_exactly_one_authenticated_revision(
    grade_http, monkeypatch
):
    ctx = grade_http
    original = repository.append_review
    barrier = asyncio.Barrier(2)

    async def together(*args, **kwargs):
        await asyncio.wait_for(barrier.wait(), timeout=10)
        return await original(*args, **kwargs)

    monkeypatch.setattr(repository, "append_review", together)
    body = payload()
    replies = await asyncio.gather(
        *(ctx.client.post(ctx.base + "/reviews", json=body) for _ in range(2))
    )
    assert sorted(r.status_code for r in replies) == [200, 201]
    assert replies[0].json()["record"] == replies[1].json()["record"]
    assert len(await records(ctx)) == 1


@pytest.mark.anyio
async def test_actual_commit_ack_loss_is_unknown_and_actor_bound_replay_recovers(
    grade_http,
    monkeypatch,
):
    from sqlalchemy.ext.asyncio import AsyncSessionTransaction
    from sqlalchemy.orm import Session

    ctx = grade_http
    original = AsyncSessionTransaction.commit

    def mark(session, context):
        if any(isinstance(row, GradeReviewRevision) for row in session.new):
            session.info["test_lose_grade_review_ack"] = True

    async def commit(transaction):
        lose = transaction.session.sync_session.info.pop("test_lose_grade_review_ack", False)
        await original(transaction)
        if lose:
            raise OperationalError("redacted", {}, RuntimeError("synthetic lost acknowledgement"))

    event.listen(Session, "after_flush", mark)
    monkeypatch.setattr(AsyncSessionTransaction, "commit", commit)
    body = payload()
    keys = set(ctx.storage.uploaded_keys)
    try:
        response = await ctx.client.post(ctx.base + "/reviews", json=body)
    finally:
        event.remove(Session, "after_flush", mark)
    assert response.status_code == 503
    assert response.json() == {"detail": "grade_review_commit_outcome_unknown"}
    rows = await records(ctx)
    assert len(rows) == 1 and rows[0].reviewer_user_id == ctx.users["a"].id
    response = await ctx.client.post(ctx.base + "/reviews", json=body)
    assert response.status_code == 200 and not response.json()["created"]
    assert response.json()["record"]["review_id"] == str(rows[0].review_id)
    assert set(ctx.storage.uploaded_keys) == keys


@pytest.mark.anyio
async def test_evidence_coordinates_are_bounded_and_link_to_authorized_canonical(grade_http):
    ctx = grade_http
    default = (await ctx.client.get(ctx.base + "/review")).json()
    assert default["evidence_locations"] == []
    response = await ctx.client.get(ctx.base + "/review?evidence_limit=1")
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["evidence_truncated"] and view["next_evidence_offset"] == 1
    location = view["evidence_locations"][0]
    segment = ctx.grade_input.selected_answer_segments[0]
    assert location["canonical_text_start"] == segment.text_start + location["text_start"]
    assert location["canonical_text_end"] == segment.text_start + location["text_end"]
    assert (await ctx.client.get(location["canonical_page_path"])).status_code == 200
    assert (
        await ctx.client.get(location["canonical_page_path"], headers=ctx.headers("b"))
    ).status_code == 404
    assert "quote" not in location and "object_key" not in response.text
    assert (await ctx.client.get(ctx.base + "/review?evidence_limit=26")).status_code == 422


@pytest.mark.anyio
async def test_cross_assignment_splice_and_broken_input_binding_are_fail_closed(grade_http):
    from test_teacher_authorization import create, question

    ctx = grade_http
    aid = await create(ctx, "b")
    qid = await question(ctx, aid, "b")
    wrong = ctx.base.replace(str(ctx.question.id), qid)
    for who in ("a", "b", "admin"):
        result = await ctx.client.get(wrong + "/review", headers=ctx.headers(who))
        assert result.status_code == 404
    async with ctx.factory() as session, session.begin():
        await session.execute(
            update(GradeProposalArtifact)
            .where(
                GradeProposalArtifact.id == ctx.proposal.proposal_id,
            )
            .values(grade_input_sha256="0" * 64)
        )
    for suffix in ("/review", "/reviews"):
        assert (await ctx.client.get(ctx.base + suffix)).status_code == 404
    assert not await records(ctx)


@pytest.mark.anyio
async def test_authorization_database_failure_cannot_read_or_append(grade_http, monkeypatch):
    from huipi_cloud.modules.identity import service as identity

    ctx = grade_http

    async def fail(*args, **kwargs):
        raise OperationalError("private database address", {}, RuntimeError("synthetic"))

    monkeypatch.setattr(identity, "refresh_principal", fail)
    response = await ctx.client.post(ctx.base + "/reviews", json=payload())
    assert response.status_code == 503 and response.json() == {
        "detail": "authorization_unavailable"
    }
    assert not await records(ctx)
