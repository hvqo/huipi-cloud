"""Signed teacher HTTP workflow over actual PG/S3, synthetic parsed source only."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from test_answer_review import _payload, _prepare_review
from test_teacher_authorization import auth_api  # noqa: F401

from huipi_cloud.modules.answer_alignment import service as alignment_service
from huipi_cloud.modules.answer_review import repository, service
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.assignments.models import Question
from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision
from huipi_cloud.modules.grading.models import GradeProposalArtifact
from huipi_cloud.modules.identity.models import AssignmentMembership


@pytest.fixture
async def review_http(auth_api, postgres_engine):  # noqa: F811
    ctx = auth_api
    previous = ctx.client.headers.copy()
    ctx.client.headers.update(ctx.headers())
    try:
        _, ctx.sid, ctx.question, ctx.region, _, ctx.bundle = await _prepare_review(
            ctx.client,
            ctx.storage,
            postgres_engine,
            content="第1题 学生合成过程：2+2=4。",
        )
        ctx.aid = ctx.bundle.context.assignment_id
        ctx.url = f"/api/v1/submissions/{ctx.sid}/answer-reviews"
        ctx.detail = f"{ctx.url}/questions/{ctx.question.id}"
        yield ctx
    finally:
        ctx.client.headers.clear()
        ctx.client.headers.update(previous)
        # This dedicated test DB is already protected by the shared fixture's _test guard.
        # Clear this test's records before its provisioned User FKs are removed.
        async with ctx.factory() as session, session.begin():
            await session.execute(text("TRUNCATE assignments CASCADE"))


async def inspect(ctx):
    response = await ctx.client.get(ctx.detail)
    assert response.status_code == 200, response.text
    return response.json()


def payload(view, decision="response_present"):
    region = view["evidence"][0]
    selection = {name: region[name] for name in ("content_pointer", "text_start", "text_end")}
    return {
        "request_id": str(uuid4()),
        "expected_revision": view["expected_revision"],
        "expected_source": view["source"],
        "decision": decision,
        "response_regions"
        if decision == "response_present"
        else "excluded_prompt_regions"
        if decision == "response_absent"
        else "uncertain_regions": [selection],
        "reason_codes": [
            "student_work_visible"
            if decision == "response_present"
            else "printed_prompt_only"
            if decision == "response_absent"
            else "ambiguous_handwriting"
        ],
    }


async def grant(ctx, name, write):
    response = await ctx.client.put(
        f"/api/v1/assignments/{ctx.aid}/members/{ctx.users[name].id}",
        headers=ctx.headers("admin"),
        json={"can_write": write},
    )
    assert response.status_code == 200


async def records(ctx):
    async with ctx.factory() as session:
        return list(
            (
                await session.scalars(
                    select(AnswerReviewDecision)
                    .where(
                        AnswerReviewDecision.submission_id == ctx.sid,
                    )
                    .order_by(AnswerReviewDecision.revision)
                )
            ).all()
        )


@pytest.mark.anyio
async def test_signed_http_inspect_present_uncertain_history_and_source_audit(review_http):
    ctx = review_http
    view = await inspect(ctx)
    assert view["answer_presence"] == "unreviewed" and view["expected_revision"] == 0
    assert not view["latest_source_matches"]
    keys = set(ctx.storage.uploaded_keys)
    async with ctx.factory() as session:
        before = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (
                GradeProposalArtifact,
                GradeReviewRevision,
            )
        ]
    first = await ctx.client.post(ctx.detail, json=payload(view))
    assert first.status_code == 201, first.text
    one = first.json()["record"]
    assert one["revision"] == 1 and one["supersedes_decision_id"] is None
    assert one["reviewer_kind"] == "authenticated_teacher"
    assert one["reviewer_user_id"] == str(ctx.users["a"].id)
    assert one["reviewer_ref"] == f"teacher_{ctx.users['a'].id.hex}"
    assert len(one["reviewer_ref"]) <= 64
    snapshot = service.decision_read((await records(ctx))[0]).model_dump(mode="json")
    view = await inspect(ctx)
    assert view["answer_presence"] == "response_present" and view["latest_source_matches"]
    second = await ctx.client.post(ctx.detail, json=payload(view, "uncertain"))
    assert second.status_code == 201
    two = second.json()["record"]
    assert two["revision"] == 2 and two["supersedes_decision_id"] == one["decision_id"]
    assert (await inspect(ctx))["answer_presence"] == "uncertain"
    assert service.decision_read((await records(ctx))[0]).model_dump(mode="json") == snapshot
    history = await ctx.client.get(
        ctx.url, params={"limit": 1, "question_id": str(ctx.question.id)}
    )
    assert history.status_code == 200
    assert history.json()["source_validity"] == "not_checked"
    assert history.json()["has_next"] and history.json()["next_offset"] == 1
    page2 = (await ctx.client.get(ctx.url, params={"offset": 1, "limit": 1})).json()
    assert not page2["has_next"] and page2["records"][0]["revision"] == 2
    assert page2["records"][0]["supersedes_decision_id"] == one["decision_id"]
    assert ctx.storage.uploaded_keys == keys
    for document in (first, second, history):
        assert "object_key" not in document.text and "s3://" not in document.text
        assert ctx.users["a"].subject not in document.text
    async with ctx.factory() as session:
        after = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (
                GradeProposalArtifact,
                GradeReviewRevision,
            )
        ]
    assert after == before


@pytest.mark.anyio
async def test_same_request_replay_conflict_and_old_revision_conflict(review_http):
    ctx = review_http
    body = payload(await inspect(ctx))
    first = await ctx.client.post(ctx.detail, json=body)
    replay = await ctx.client.post(ctx.detail, json=body)
    assert first.status_code == 201 and replay.status_code == 200
    assert first.json()["record"] == replay.json()["record"]
    assert not replay.json()["created"]
    changed = deepcopy(body)
    changed["reason_codes"] = ["multiple_response_regions"]
    assert (await ctx.client.post(ctx.detail, json=changed)).json() == {
        "detail": "review_request_conflict",
    }
    body["request_id"] = str(uuid4())
    stale = await ctx.client.post(ctx.detail, json=body)
    assert stale.status_code == 409 and stale.json()["detail"] == "review_revision_conflict"
    assert len(await records(ctx)) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field",
    ["reviewer_ref", "reviewer_id", "reviewer_user_id", "teacher_id", "assignment_id", "role"],
)
async def test_client_cannot_supply_identity_fields(review_http, field):
    ctx = review_http
    body = payload(await inspect(ctx))
    body[field] = "admin"
    response = await ctx.client.post(ctx.detail, json=body)
    assert response.status_code == 422
    assert not await records(ctx)


@pytest.mark.anyio
async def test_reader_writer_foreign_teacher_expiry_and_revocation(review_http):
    ctx = review_http
    body = payload(await inspect(ctx))
    for headers, expected in [
        (ctx.headers("b", role="admin"), 404),
        ({"Authorization": ""}, 401),
        (ctx.headers(exp=1), 401),
    ]:
        assert (await ctx.client.get(ctx.detail, headers=headers)).status_code == expected
        assert (
            await ctx.client.post(ctx.detail, headers=headers, json=body)
        ).status_code == expected
    await grant(ctx, "reader", False)
    assert (await ctx.client.get(ctx.detail, headers=ctx.headers("reader"))).status_code == 200
    assert (
        await ctx.client.post(ctx.detail, headers=ctx.headers("reader"), json=body)
    ).status_code == 404
    await grant(ctx, "reader", True)
    result = await ctx.client.post(ctx.detail, headers=ctx.headers("reader"), json=body)
    assert result.status_code == 201
    assert result.json()["record"]["reviewer_user_id"] == str(ctx.users["reader"].id)
    await ctx.client.delete(
        f"/api/v1/assignments/{ctx.aid}/members/{ctx.users['reader'].id}",
        headers=ctx.headers("admin"),
    )
    assert (
        await ctx.client.post(ctx.detail, headers=ctx.headers("reader"), json=body)
    ).status_code == 404
    assert len(await records(ctx)) == 1


@pytest.mark.anyio
async def test_concurrent_two_teachers_one_expected_revision_wins(review_http, monkeypatch):
    ctx = review_http
    await grant(ctx, "b", True)
    body = payload(await inspect(ctx))
    other = {**body, "request_id": str(uuid4())}
    original = repository.append_decision
    barrier = asyncio.Barrier(2)

    async def synchronized(*args, **kwargs):
        await asyncio.wait_for(barrier.wait(), timeout=5)
        return await original(*args, **kwargs)

    monkeypatch.setattr(repository, "append_decision", synchronized)
    results = await asyncio.gather(
        ctx.client.post(ctx.detail, json=body),
        ctx.client.post(ctx.detail, json=other, headers=ctx.headers("b")),
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    rows = await records(ctx)
    assert len(rows) == 1 and rows[0].revision == 1
    assert rows[0].reviewer_user_id in {ctx.users["a"].id, ctx.users["b"].id}
    monkeypatch.setattr(repository, "append_decision", original)
    current = await inspect(ctx)
    second = await ctx.client.post(
        ctx.detail, json=payload(current, "uncertain"), headers=ctx.headers("b")
    )
    assert second.status_code == 201 and second.json()["record"]["revision"] == 2
    assert second.json()["record"]["supersedes_decision_id"] == str(rows[0].id)


@pytest.mark.anyio
async def test_precondition_and_real_current_source_change_do_not_bind_old_selections(review_http):
    ctx = review_http
    old = await inspect(ctx)
    body = payload(old)
    bad = deepcopy(body)
    bad["expected_source"]["canonical_sha256"] = "0" * 64
    assert (await ctx.client.post(ctx.detail, json=bad)).status_code == 409
    assert (await ctx.client.post(ctx.detail, json=body)).status_code == 201
    async with ctx.factory() as session, session.begin():
        await session.execute(
            update(Question)
            .where(Question.id == ctx.question.id)
            .values(
                question_type="essay",
            )
        )
    await alignment_service.align_submission(ctx.factory, ctx.storage, ctx.sid)
    current = await inspect(ctx)
    assert current["answer_presence"] == "unreviewed"
    assert not current["latest_source_matches"] and current["expected_revision"] == 1
    body["request_id"] = str(uuid4())
    assert (await ctx.client.post(ctx.detail, json=body)).status_code == 409
    response = await ctx.client.post(ctx.detail, json=payload(current, "uncertain"))
    assert response.status_code == 201 and response.json()["record"]["revision"] == 2
    assert len(await records(ctx)) == 2


@pytest.mark.anyio
async def test_bad_unicode_ranges_and_unknown_nodes_rejected(review_http):
    ctx = review_http
    body = payload(await inspect(ctx))
    for replacement in [{"text_end": 999999}, {"content_pointer": "/pages/0/blocks/99/content"}]:
        invalid = deepcopy(body)
        invalid["response_regions"][0].update(replacement)
        response = await ctx.client.post(ctx.detail, json=invalid)
        assert response.status_code == 422
        assert response.json() == {"detail": "review_region_invalid"}
    assert not await records(ctx)


@pytest.mark.anyio
async def test_mid_io_revocation_blocks_commit_instead_of_using_old_request_auth(
    review_http, monkeypatch
):
    ctx = review_http
    body = payload(await inspect(ctx))
    original = repository.append_decision
    reached, release = asyncio.Event(), asyncio.Event()

    async def pause(*args, **kwargs):
        reached.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        return await original(*args, **kwargs)

    monkeypatch.setattr(repository, "append_decision", pause)
    post = asyncio.create_task(ctx.client.post(ctx.detail, json=body))
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        revoke = await ctx.client.delete(
            f"/api/v1/assignments/{ctx.aid}/members/{ctx.users['a'].id}",
            headers=ctx.headers("admin"),
        )
        assert revoke.status_code == 204
    finally:
        release.set()
    response = await post
    assert response.status_code == 404 and not await records(ctx)


@pytest.mark.anyio
async def test_commit_permission_locks_serialize_with_revocation(review_http, monkeypatch):
    ctx = review_http
    body = payload(await inspect(ctx))
    original = repository.require_assignment
    reached, release = asyncio.Event(), asyncio.Event()

    async def locked(*args, **kwargs):
        await original(*args, **kwargs)
        reached.set()
        await asyncio.wait_for(release.wait(), timeout=10)

    monkeypatch.setattr(repository, "require_assignment", locked)
    post = asyncio.create_task(ctx.client.post(ctx.detail, json=body))
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
                        .values(revoked_at=func.now())
                    )
    finally:
        release.set()
    assert (await post).status_code == 201
    assert len(await records(ctx)) == 1


@pytest.mark.anyio
async def test_database_insert_rollback_and_unknown_commit_replay(
    review_http, monkeypatch, postgres_engine
):
    ctx = review_http
    body = payload(await inspect(ctx))

    def fail_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO ANSWER_REVIEW_DECISIONS"):
            raise OperationalError("redacted", {}, RuntimeError("synthetic failure"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail_insert)
    try:
        result = await ctx.client.post(ctx.detail, json=body)
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail_insert)
    assert result.status_code == 503 and not await records(ctx)
    original = repository.append_decision

    async def lost_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise OperationalError("redacted", {}, RuntimeError("synthetic lost COMMIT ack"))

    monkeypatch.setattr(repository, "append_decision", lost_ack)
    assert (await ctx.client.post(ctx.detail, json=body)).status_code == 503
    assert len(await records(ctx)) == 1
    monkeypatch.setattr(repository, "append_decision", original)
    retry = await ctx.client.post(ctx.detail, json=body)
    assert retry.status_code == 200 and not retry.json()["created"]
    assert len(await records(ctx)) == 1


@pytest.mark.anyio
async def test_pg_refuses_update_delete_and_legacy_claim_is_not_upgraded(review_http):
    ctx = review_http
    row, _ = await service.record_decision(
        ctx.factory,
        ctx.storage,
        submission_id=ctx.sid,
        question_id=ctx.question.id,
        request_id=uuid4(),
        payload=_payload(
            "response_present", ctx.region, reviewer=f"teacher_{ctx.users['a'].id.hex}"
        ),
    )
    legacy = (await ctx.client.get(ctx.url)).json()["records"][0]
    assert legacy["reviewer_kind"] == "legacy_cli_claim" and legacy["reviewer_user_id"] is None
    for stmt in [
        update(AnswerReviewDecision)
        .where(AnswerReviewDecision.id == row.decision_id)
        .values(reviewer_ref="tampered"),
        delete(AnswerReviewDecision).where(AnswerReviewDecision.id == row.decision_id),
    ]:
        async with ctx.factory() as session:
            with pytest.raises(IntegrityError):
                async with session.begin():
                    await session.execute(stmt)
    stored = (await records(ctx))[0]
    assert stored.reviewer_ref == legacy["reviewer_ref"] and stored.revision == 1


@pytest.mark.anyio
async def test_cross_assignment_question_path_and_filter_are_rejected_even_for_admin(review_http):
    ctx = review_http
    async with ctx.factory() as session, session.begin():
        from huipi_cloud.modules.assignments.models import Assignment

        assignment = Assignment(title="other scope", subject="math", grade_level="10")
        session.add(assignment)
        await session.flush()
        question = Question(
            assignment_id=assignment.id,
            question_number=1,
            question_type="short_answer",
            stem="other",
            max_score=1,
        )
        session.add(question)
        await session.flush()
        qid = question.id
    for who in ("a", "admin"):
        headers = ctx.headers(who)
        wrong = f"{ctx.url}/questions/{qid}"
        assert (await ctx.client.get(wrong, headers=headers)).status_code == 404
        assert (
            await ctx.client.post(wrong, headers=headers, json=payload(await inspect(ctx)))
        ).status_code == 404
        assert (
            await ctx.client.get(ctx.url, headers=headers, params={"question_id": str(qid)})
        ).status_code == 404
    assert not await records(ctx)


@pytest.mark.anyio
async def test_list_bounds_empty_page_and_safe_canonical_page_access(review_http):
    ctx = review_http
    view = await inspect(ctx)
    page = await ctx.client.get(view["evidence"][0]["canonical_page_path"])
    assert page.status_code == 200 and "2+2=4" in page.text
    assert (await ctx.client.get(ctx.url, params={"limit": 26})).status_code == 422
    assert (await ctx.client.get(ctx.url, params={"offset": -1})).status_code == 422
    empty = (await ctx.client.get(ctx.url, params={"offset": 99})).json()
    assert empty["records"] == [] and not empty["has_next"] and empty["next_offset"] is None
    end = (await ctx.client.get(ctx.detail, params={"evidence_offset": 999})).json()
    assert end["evidence"] == [] and not end["evidence_truncated"]


@pytest.mark.anyio
async def test_corrupted_canonical_object_is_503_without_success_record(review_http):
    from io import BytesIO

    ctx = review_http
    body = payload(await inspect(ctx))
    key = ctx.bundle.canonical_artifact.canonical_object_key
    await ctx.storage.upload_fileobj(BytesIO(b"synthetic corruption"), key, "application/json")
    response = await ctx.client.post(ctx.detail, json=body)
    assert response.status_code == 503
    assert "corruption" not in response.text and key not in response.text
    assert not await records(ctx)


@pytest.mark.anyio
async def test_source_changed_between_verification_and_append_is_409(review_http, monkeypatch):
    from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact

    ctx = review_http
    body = payload(await inspect(ctx))
    original = repository.append_decision

    async def change_then_append(*args, **kwargs):
        async with ctx.factory() as session, session.begin():
            await session.execute(
                update(CanonicalArtifact)
                .where(
                    CanonicalArtifact.id == ctx.bundle.canonical_artifact.id,
                )
                .values(canonical_sha256="0" * 64)
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(repository, "append_decision", change_then_append)
    response = await ctx.client.post(ctx.detail, json=body)
    assert response.status_code == 409 and response.json()["detail"] == "review_source_changed"
    assert not await records(ctx)


@pytest.mark.anyio
async def test_disabling_teacher_after_io_prevents_append(review_http, monkeypatch):
    from huipi_cloud.modules.identity.models import User

    ctx = review_http
    body = payload(await inspect(ctx))
    original = repository.append_decision

    async def disable_then_append(*args, **kwargs):
        async with ctx.factory() as session, session.begin():
            await session.execute(
                update(User).where(User.id == ctx.users["a"].id).values(is_active=False)
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(repository, "append_decision", disable_then_append)
    response = await ctx.client.post(ctx.detail, json=body)
    assert response.status_code == 403 and not await records(ctx)


@pytest.mark.anyio
async def test_review_migration_roundtrip_preserves_legacy_record_and_trigger(
    client,
    postgres_engine,
    recording_minio_storage,
):
    """A new UUID database, real source fixtures, and downgrade of ONLY that database."""
    import os
    import subprocess
    import sys

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from huipi_cloud.infrastructure.database.session import get_db_session
    from huipi_cloud.main import app
    from huipi_cloud.modules.identity.dependencies import authorize_request
    from huipi_cloud.modules.identity.models import User

    base_url = postgres_engine.url
    name = f"huipi_review_migration_{uuid4().hex}_test"
    target = base_url.set(database=name)
    admin = create_async_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine = create_async_engine(target)
    created = False
    previous = app.dependency_overrides[get_db_session]
    environment = dict(os.environ, DATABASE_URL=target.render_as_string(hide_password=False))

    async def migrate(*args):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", *args],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr[-3000:]

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

        async def scratch_session():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db_session] = scratch_session
        from huipi_cloud.infrastructure.storage.dependencies import get_object_storage

        old_storage = app.dependency_overrides.get(get_object_storage)
        app.dependency_overrides[get_object_storage] = lambda: recording_minio_storage
        try:
            _, sid, question, region, _, _ = await _prepare_review(
                client,
                recording_minio_storage,
                engine,
            )
            legacy, _ = await service.record_decision(
                factory,
                recording_minio_storage,
                submission_id=sid,
                question_id=question.id,
                request_id=uuid4(),
                payload=_payload("response_present", region),
            )
        finally:
            if old_storage is None:
                app.dependency_overrides.pop(get_object_storage, None)
            else:
                app.dependency_overrides[get_object_storage] = old_storage
        app.dependency_overrides[get_db_session] = previous
        async with engine.connect() as connection:
            before = await connection.scalar(
                text(
                    "SELECT to_jsonb(r) - 'reviewer_user_id' "
                    "FROM answer_review_decisions r WHERE id=:id"
                ),
                {"id": legacy.decision_id},
            )
        await engine.dispose()
        await migrate("downgrade", "6a9d8c7e2f10")
        async with engine.connect() as connection:
            old = await connection.scalar(
                text("SELECT to_jsonb(r) FROM answer_review_decisions r WHERE id=:id"),
                {"id": legacy.decision_id},
            )
        assert old == before
        await engine.dispose()
        await migrate("upgrade", "head")
        await migrate("check")
        async with factory() as session:
            restored = await session.get(AnswerReviewDecision, legacy.decision_id)
            assert restored.reviewer_user_id is None
            assert service.decision_read(restored) == legacy
        async with factory() as session:
            with pytest.raises(IntegrityError):
                async with session.begin():
                    await session.execute(
                        update(AnswerReviewDecision)
                        .where(
                            AnswerReviewDecision.id == legacy.decision_id,
                        )
                        .values(reviewer_ref="forbidden")
                    )
    finally:
        app.dependency_overrides[get_db_session] = previous
        await engine.dispose()
        if created:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.anyio
async def test_idempotency_key_cannot_be_replayed_as_another_authenticated_teacher(review_http):
    ctx = review_http
    await grant(ctx, "b", True)
    body = payload(await inspect(ctx))
    first = await ctx.client.post(ctx.detail, json=body)
    assert first.status_code == 201
    replay = await ctx.client.post(ctx.detail, json=body, headers=ctx.headers("b"))
    assert replay.status_code == 409 and replay.json()["detail"] == "review_request_conflict"
    stored = await records(ctx)
    assert len(stored) == 1 and stored[0].reviewer_user_id == ctx.users["a"].id
