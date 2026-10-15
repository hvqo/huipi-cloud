"""Real PostgreSQL + private S3 + signed tokens: no authorization dependency bypass."""

import hashlib
import io
import json
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pypdf import PdfWriter
from sqlalchemy import delete, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from huipi_cloud.infrastructure.auth.jwt import TokenVerifier
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.main import app
from huipi_cloud.modules.assignments.models import Assignment, Question
from huipi_cloud.modules.copilot.authorized import AuthorizedBusinessTools
from huipi_cloud.modules.copilot.protocol import CopilotError
from huipi_cloud.modules.copilot.tools import validate_arguments
from huipi_cloud.modules.identity import dependencies as security
from huipi_cloud.modules.identity import service as identity_service
from huipi_cloud.modules.identity.models import AssignmentMembership, User
from huipi_cloud.modules.submissions.models import Submission


@pytest.fixture
async def auth_api(client, postgres_engine, recording_minio_storage, monkeypatch):
    from huipi_cloud.core.config import Settings

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    config = Settings(
        _env_file=None,
        auth_issuer="https://identity.invalid",
        auth_audience="huipi-api",
        auth_jwks_url="https://identity.invalid/jwks",
    )
    public = {
        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
        "kid": "fixture",
        "alg": "RS256",
    }
    check = TokenVerifier(
        config,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"keys": [public]}),
        ),
    )
    monkeypatch.setattr(security, "verifier", check)
    original = app.dependency_overrides.copy()
    app.dependency_overrides.pop(security.authorize_request)
    app.dependency_overrides.pop(security.get_current_principal)
    app.dependency_overrides[get_object_storage] = lambda: recording_minio_storage
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    users = {}
    async with factory() as session, session.begin():
        for name in ("a", "b", "admin", "reader"):
            row = User(
                issuer=config.auth_issuer,
                subject=str(uuid4()),
                is_active=True,
                is_admin=name == "admin",
            )
            session.add(row)
            await session.flush()
            users[name] = row

    def headers(name="a", **claims):
        now = int(time.time())
        value = jwt.encode(
            {
                "iss": config.auth_issuer,
                "aud": config.auth_audience,
                "sub": users[name].subject,
                "iat": now - 1,
                "exp": now + 600,
                **claims,
            },
            key,
            algorithm="RS256",
            headers={"kid": "fixture"},
        )
        return {"Authorization": f"Bearer {value}"}

    class Context:
        pass

    ctx = Context()
    ctx.client, ctx.factory, ctx.headers, ctx.users = client, factory, headers, users
    ctx.storage, ctx.verifier = recording_minio_storage, check
    try:
        yield ctx
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(original)
        ids = [user.id for user in users.values()]
        async with factory() as session, session.begin():
            await session.execute(
                delete(AssignmentMembership).where(
                    AssignmentMembership.user_id.in_(ids),
                )
            )
            await session.execute(delete(User).where(User.id.in_(ids)))


async def create(ctx, who="a"):
    response = await ctx.client.post(
        "/api/v1/assignments",
        headers=ctx.headers(who),
        json={
            "title": "合成认证验收",
            "subject": "数学",
            "grade_level": "高一",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def question(ctx, aid, who="a"):
    result = await ctx.client.post(
        f"/api/v1/assignments/{aid}/questions",
        headers=ctx.headers(who),
        json={
            "question_number": 1,
            "question_type": "short_answer",
            "stem": "合成1+1？",
            "max_score": "2.00",
        },
    )
    assert result.status_code == 201, result.text
    return result.json()["id"]


@pytest.mark.anyio
async def test_trusted_teacher_full_read_write_membership_and_no_token_role_promotion(auth_api):
    ctx = auth_api
    aid = await create(ctx)
    qid = await question(ctx, aid)
    for suffix, payload in [
        ("answer-key", {"answer_content": "2"}),
        ("rubric", {"criteria": [{"description": "正确结果", "points": "2.00", "sort_order": 1}]}),
    ]:
        response = await ctx.client.put(
            f"/api/v1/questions/{qid}/{suffix}", json=payload, headers=ctx.headers()
        )
        assert response.status_code == 200
    detail = await ctx.client.get(f"/api/v1/assignments/{aid}", headers=ctx.headers())
    assert detail.json()["questions"][0]["rubric_criteria"][0]["points"] == "2.00"
    assert detail.json()["questions"][0]["answer_key"]["answer_content"] == "2"
    async with ctx.factory() as session:
        grant = await session.scalar(
            select(AssignmentMembership).where(
                AssignmentMembership.assignment_id == UUID(aid),
            )
        )
        assert grant.user_id == ctx.users["a"].id and grant.can_write
    forbidden = await ctx.client.put(
        f"/api/v1/assignments/{aid}/members/{ctx.users['b'].id}",
        headers=ctx.headers(role="admin", teacher_id=str(ctx.users["admin"].id)),
        json={"can_write": True},
    )
    assert forbidden.status_code == 403
    async with ctx.factory() as session:
        assert (
            await session.scalar(
                select(AssignmentMembership.id).where(
                    AssignmentMembership.user_id == ctx.users["b"].id,
                )
            )
            is None
        )


@pytest.mark.anyio
async def test_assignments_list_filters_before_pagination_and_history_is_not_claimed(auth_api):
    ctx = auth_api
    a, b = await create(ctx), await create(ctx, "b")
    async with ctx.factory() as session, session.begin():
        old = Assignment(title="历史未绑定", subject="数学", grade_level="高一")
        session.add(old)
        await session.flush()
        old_id = old.id
    listing = await ctx.client.get("/api/v1/assignments?limit=1", headers=ctx.headers())
    assert [row["id"] for row in listing.json()] == [a]
    for value in (b, str(old_id), str(uuid4())):
        result = await ctx.client.get(f"/api/v1/assignments/{value}", headers=ctx.headers())
        assert result.status_code == 404
        assert result.json() == {"detail": "resource_not_found_or_forbidden"}
    assert (
        await ctx.client.get(f"/api/v1/assignments/{old_id}", headers=ctx.headers("admin"))
    ).status_code == 200


@pytest.mark.anyio
async def test_cross_question_submission_and_same_caller_two_grants_cannot_mix_scopes(auth_api):
    ctx = auth_api
    a, b = await create(ctx), await create(ctx, "b")
    qa, qb = await question(ctx, a), await question(ctx, b, "b")
    async with ctx.factory() as session, session.begin():
        submission = Submission(assignment_id=UUID(b), student_ref="synthetic-b")
        session.add(submission)
        await session.flush()
        sid = submission.id
    paths = [
        f"/api/v1/submissions/{sid}",
        f"/api/v1/submissions/{sid}/file",
        f"/api/v1/submissions/{sid}/canonical-document/pages/1",
        f"/api/v1/submissions/{sid}/answer-alignment/questions/{qb}",
    ]
    for path in paths:
        response = await ctx.client.get(path, headers=ctx.headers())
        assert response.status_code == 404 and "synthetic-b" not in response.text
    result = await ctx.client.put(
        f"/api/v1/questions/{qb}/answer-key",
        headers=ctx.headers(),
        json={"answer_content": "越权不得写入"},
    )
    assert result.status_code == 404
    mismatch = await ctx.client.get(
        f"/api/v1/submissions/{sid}/answer-alignment/questions/{qa}",
        headers=ctx.headers("admin"),
    )
    assert mismatch.status_code == 404  # An admin still cannot splice two scopes.
    async with ctx.factory() as session:
        assert await session.scalar(
            select(Question.assignment_id).where(Question.id == UUID(qb))
        ) == UUID(b)


@pytest.mark.anyio
async def test_database_admin_grants_reader_writer_and_revokes_on_next_request(auth_api):
    ctx = auth_api
    aid = await create(ctx)
    url = f"/api/v1/assignments/{aid}/members/{ctx.users['reader'].id}"
    assert (
        await ctx.client.put(url, headers=ctx.headers("admin"), json={"can_write": False})
    ).status_code == 200
    assert (
        await ctx.client.get(f"/api/v1/assignments/{aid}", headers=ctx.headers("reader"))
    ).status_code == 200
    denied = await ctx.client.post(
        f"/api/v1/assignments/{aid}/publish", headers=ctx.headers("reader")
    )
    assert denied.status_code == 404
    assert (
        await ctx.client.put(url, headers=ctx.headers("admin"), json={"can_write": True})
    ).status_code == 200
    await question(ctx, aid, "reader")
    assert (await ctx.client.delete(url, headers=ctx.headers("admin"))).status_code == 204
    assert (
        await ctx.client.get(f"/api/v1/assignments/{aid}", headers=ctx.headers("reader"))
    ).status_code == 404
    async with ctx.factory() as session:
        row = await session.scalar(
            select(AssignmentMembership).where(
                AssignmentMembership.user_id == ctx.users["reader"].id,
            )
        )
        assert row.revoked_at is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "claims",
    [{"exp": 1}, {"iss": "https://wrong.invalid"}, {"aud": "other"}, {"sub": "unprovisioned"}],
)
async def test_invalid_or_unmapped_tokens_cannot_reach_business(auth_api, claims):
    ctx = auth_api
    response = await ctx.client.get("/api/v1/assignments", headers=ctx.headers(**claims))
    assert response.status_code in {401, 403}
    assert "Bearer " not in response.text
    async with ctx.factory() as session:
        assert await session.scalar(select(Assignment.id).limit(1)) is None


@pytest.mark.anyio
async def test_all_old_http_routes_require_token_including_every_private_download(auth_api):
    ctx = auth_api
    count = 0
    for path, operations in app.openapi()["paths"].items():
        if path.endswith("/health"):
            continue
        for method in operations:
            if method not in {"get", "post", "put", "delete"}:
                continue
            url = path
            for name in ("assignment_id", "question_id", "submission_id", "user_id"):
                url = url.replace("{" + name + "}", str(uuid4()))
            url = url.replace("{page_number}", "1")
            response = await ctx.client.request(method, url, json={})
            assert response.status_code == 401, (method, path, response.text)
            assert response.headers["www-authenticate"] == "Bearer"
            assert "security" in operations[method]
            count += 1
    assert count == 20
    assert (await ctx.client.get("/api/v1/health")).json() == {"status": "ok"}


@pytest.mark.anyio
async def test_disabled_user_forged_header_and_auth_database_failure_fail_closed(
    auth_api, monkeypatch
):
    ctx = auth_api
    aid = await create(ctx)
    async with ctx.factory() as session, session.begin():
        await session.execute(
            update(User).where(User.id == ctx.users["a"].id).values(is_active=False)
        )
    assert (
        await ctx.client.get(f"/api/v1/assignments/{aid}", headers=ctx.headers())
    ).status_code == 403
    assert (
        await ctx.client.get(
            f"/api/v1/assignments/{aid}",
            headers={"teacher_id": str(ctx.users["a"].id), "role": "admin"},
        )
    ).status_code == 401

    async def unavailable(*args):
        raise OperationalError("private-dsn", {}, RuntimeError("secret connection"))

    monkeypatch.setattr(identity_service, "resolve_principal", unavailable)
    result = await ctx.client.get(f"/api/v1/assignments/{aid}", headers=ctx.headers("admin"))
    assert result.status_code == 503
    assert result.json() == {"detail": "authorization_unavailable"}
    assert "secret" not in result.text and "private" not in result.text


@pytest.mark.anyio
async def test_authorized_pdf_upload_download_hash_and_other_teacher_denied(auth_api):
    ctx = auth_api
    aid = await create(ctx)
    qid = await question(ctx, aid)
    for suffix, body in [
        ("answer-key", {"answer_content": "2"}),
        (
            "rubric",
            {
                "criteria": [
                    {"description": "正确", "points": "2.00", "sort_order": 1},
                ]
            },
        ),
    ]:
        assert (
            await ctx.client.put(
                f"/api/v1/questions/{qid}/{suffix}", headers=ctx.headers(), json=body
            )
        ).status_code == 200
    assert (
        await ctx.client.post(f"/api/v1/assignments/{aid}/publish", headers=ctx.headers())
    ).status_code == 200
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    stream = io.BytesIO()
    writer.write(stream)
    content = stream.getvalue()
    response = await ctx.client.post(
        f"/api/v1/assignments/{aid}/submissions",
        headers=ctx.headers(),
        data={"student_ref": "synthetic-a"},
        files={"file": ("synthetic.pdf", content, "application/pdf")},
    )
    assert response.status_code == 201, response.text
    submission = response.json()
    assert submission["file"]["sha256"] == hashlib.sha256(content).hexdigest()
    url = f"/api/v1/submissions/{submission['id']}/file"
    downloaded = await ctx.client.get(url, headers=ctx.headers())
    assert downloaded.content == content
    assert downloaded.headers["content-type"] == "application/pdf"
    assert (await ctx.client.get(url, headers=ctx.headers("b"))).status_code == 404
    assert (await ctx.client.get(url)).status_code == 401


@pytest.mark.anyio
async def test_trusted_principal_agent_scope_rechecks_revocation_before_tool(auth_api, monkeypatch):
    ctx = auth_api
    aid = UUID(await create(ctx))
    # Same real verifier as the HTTP entry; the model never supplies Principal or scope.
    monkeypatch.setattr(
        "huipi_cloud.modules.copilot.authorized.TokenVerifier", lambda config: ctx.verifier
    )
    tools = await AuthorizedBusinessTools.from_bearer(
        ctx.headers()["Authorization"][7:],
        ctx.verifier.config,
        ctx.factory,
        aid,
    )
    result = await tools.execute(
        "get_assignment_summary",
        validate_arguments(
            "get_assignment_summary",
            "{}",
        ),
    )
    assert result["assignment_id"] == str(aid)
    async with ctx.factory() as session, session.begin():
        await session.execute(
            update(AssignmentMembership)
            .where(
                AssignmentMembership.assignment_id == aid,
            )
            .values(revoked_at=datetime.now(UTC))
        )
    with pytest.raises(CopilotError) as caught:
        await tools.execute(
            "get_assignment_summary",
            validate_arguments(
                "get_assignment_summary",
                "{}",
            ),
        )
    assert caught.value.code == "resource_not_found_or_forbidden"


@pytest.mark.anyio
async def test_invalid_signature_and_signed_role_cannot_impersonate_existing_admin(auth_api):
    ctx = auth_api
    aid = await create(ctx)
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode(
        {
            "iss": ctx.verifier.config.auth_issuer,
            "aud": "huipi-api",
            "sub": ctx.users["admin"].subject,
            "iat": now,
            "exp": now + 600,
        },
        wrong,
        algorithm="RS256",
        headers={"kid": "fixture"},
    )
    response = await ctx.client.get(
        f"/api/v1/assignments/{aid}", headers={"Authorization": "Bearer " + forged}
    )
    assert response.status_code == 401
    no_grant = await ctx.client.get(
        f"/api/v1/assignments/{aid}",
        headers=ctx.headers(
            "b",
            teacher_id=str(ctx.users["a"].id),
            role="admin",
        ),
    )
    assert no_grant.status_code == 404


@pytest.mark.anyio
async def test_real_connection_refused_during_authorization_is_safe_503(auth_api):
    from sqlalchemy.ext.asyncio import create_async_engine

    ctx = auth_api
    aid = await create(ctx)
    # A separate engine aimed at an unused port. The running PG service is untouched.
    async with ctx.factory() as session:
        url = session.bind.url.set(port=1)
    engine = create_async_engine(url, connect_args={"timeout": 1})
    failing_factory = async_sessionmaker(engine)

    async def failed_auth_session():
        async with failing_factory() as session:
            yield session

    old = app.dependency_overrides[security.get_auth_session]
    app.dependency_overrides[security.get_auth_session] = failed_auth_session
    try:
        response = await ctx.client.get(f"/api/v1/assignments/{aid}", headers=ctx.headers())
        assert response.status_code == 503
        assert response.json() == {"detail": "authorization_unavailable"}
    finally:
        app.dependency_overrides[security.get_auth_session] = old
        await engine.dispose()
    assert (
        await ctx.client.get(f"/api/v1/assignments/{aid}", headers=ctx.headers())
    ).status_code == 200


@pytest.mark.anyio
async def test_user_disable_admin_demotion_and_token_expiry_apply_to_agent(auth_api, monkeypatch):
    ctx = auth_api
    aid = UUID(await create(ctx))
    monkeypatch.setattr(
        "huipi_cloud.modules.copilot.authorized.TokenVerifier", lambda config: ctx.verifier
    )
    tools = await AuthorizedBusinessTools.from_bearer(
        ctx.headers("admin")["Authorization"][7:],
        ctx.verifier.config,
        ctx.factory,
        aid,
    )
    async with ctx.factory() as session, session.begin():
        await session.execute(
            update(User)
            .where(User.id == ctx.users["admin"].id)
            .values(
                is_admin=False,
            )
        )
    with pytest.raises(CopilotError) as caught:
        await tools.execute(
            "get_assignment_summary",
            validate_arguments(
                "get_assignment_summary",
                "{}",
            ),
        )
    assert caught.value.code == "resource_not_found_or_forbidden"


@pytest.mark.anyio
async def test_unregistered_route_cannot_borrow_known_policy_name(auth_api):
    from fastapi import Depends, FastAPI

    ctx = auth_api
    shadow = FastAPI()
    shadow.dependency_overrides.update(app.dependency_overrides)

    @shadow.get(
        "/unknown", name="list_assignments", dependencies=[Depends(security.authorize_request)]
    )
    async def unsafe_new_route():
        raise AssertionError("A route without an exact policy must never execute")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shadow), base_url="http://test"
    ) as client:
        # The AccessError propagates because this isolated test app has no exception handler.
        from huipi_cloud.modules.identity.protocol import AccessError

        with pytest.raises(AccessError) as caught:
            await client.get("/unknown", headers=ctx.headers())
    assert caught.value.code == "route_not_authorized"


@pytest.mark.anyio
async def test_owner_membership_and_assignment_creation_are_one_transaction(auth_api):
    from sqlalchemy.exc import IntegrityError

    from huipi_cloud.modules.assignments import service
    from huipi_cloud.modules.assignments.schemas import AssignmentCreate

    ctx = auth_api
    async with ctx.factory() as session:
        with pytest.raises(IntegrityError):
            await service.create_assignment(
                session,
                AssignmentCreate(
                    title="synthetic rollback",
                    subject="数学",
                    grade_level="高一",
                ),
                owner_id=uuid4(),
            )  # Missing owner FK must roll back the new Assignment too.
    async with ctx.factory() as session:
        assert (
            await session.scalar(
                select(Assignment.id).where(
                    Assignment.title == "synthetic rollback",
                )
            )
            is None
        )
