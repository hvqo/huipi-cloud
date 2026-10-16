"""Signed JWTs + real isolated PostgreSQL: server roles, administration and audit integrity."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from test_teacher_authorization import auth_api, create, question  # noqa: F401

from huipi_cloud.modules.assignments.models import Question
from huipi_cloud.modules.identity import admin_service, service
from huipi_cloud.modules.identity.http_schemas import TeacherActivation
from huipi_cloud.modules.identity.models import AdminOperation, AssignmentMembership, User
from huipi_cloud.modules.identity.protocol import AccessError


@pytest.fixture
async def admin_api(auth_api, monkeypatch):  # noqa: F811
    ctx = auth_api
    from huipi_cloud.core.config import Settings

    monkeypatch.setattr(
        admin_service,
        "settings",
        Settings(
            _env_file=None,
            auth_issuer="https://identity.invalid",
        ),
    )
    ctx.registered = []
    yield ctx
    async with ctx.factory() as session, session.begin():
        await session.execute(delete(User).where(User.id.in_(ctx.registered)))


def registration(**extra):
    return {
        "request_id": str(uuid4()),
        "issuer": "https://identity.invalid",
        "subject": "synthetic-" + uuid4().hex,
        **extra,
    }


async def register(ctx, payload=None):
    body = payload or registration()
    response = await ctx.client.post(
        "/api/v1/admin/teachers", headers=ctx.headers("admin"), json=body
    )
    assert response.status_code == 201, response.text
    uid = UUID(response.json()["target_user_id"])
    if uid not in ctx.registered:
        ctx.registered.append(uid)
    return response.json(), body


@pytest.mark.anyio
async def test_me_is_database_projection_and_contains_no_oidc_identity(admin_api):
    ctx = admin_api
    for who, role in [("a", "teacher"), ("admin", "admin")]:
        response = await ctx.client.get(
            "/api/v1/me",
            headers=ctx.headers(
                who,
                role="admin",
                is_admin=True,
                teacher_id=str(ctx.users["admin"].id),
            ),
        )
        assert response.status_code == 200
        value = response.json()
        assert value["role"] == role and value["user_id"] == str(ctx.users[who].id)
        assert value["is_active"] is True and value["display_name"] is None
        assert ("teacher:manage" in value["capabilities"]) == (role == "admin")
        assert ("assignment:access_all" in value["capabilities"]) == (role == "admin")
        assert not {"issuer", "subject", "token", "is_admin"} & value.keys()
        assert response.headers["cache-control"] == "private, no-store"
    assert (await ctx.client.get("/api/v1/me")).status_code == 401


@pytest.mark.anyio
async def test_teacher_cannot_admin_even_with_signed_role_or_actor_claim(admin_api):
    ctx = admin_api
    uid = ctx.users["b"].id
    requests = [
        ("GET", "/admin/teachers", None),
        ("POST", "/admin/teachers", registration()),
        ("PATCH", f"/admin/teachers/{uid}", {"request_id": str(uuid4()), "is_active": False}),
        ("GET", f"/admin/teachers/{uid}/memberships", None),
        ("GET", "/admin/audit-events", None),
    ]
    for method, path, body in requests:
        response = await ctx.client.request(
            method,
            "/api/v1" + path,
            json=body,
            headers=ctx.headers(role="admin", is_admin=True, teacher_id=str(ctx.users["admin"].id)),
        )
        assert response.status_code == 403 and response.json()["detail"] == "administrator_required"
    async with ctx.factory() as session:
        assert (await session.get(User, uid)).is_active
        assert await session.scalar(select(func.count()).select_from(AdminOperation)) == 0


@pytest.mark.anyio
async def test_register_replay_and_safe_paginated_teacher_list(admin_api):
    ctx = admin_api
    result, body = await register(ctx)
    same, _ = await register(ctx, body)
    assert same == result
    conflict = await ctx.client.post(
        "/api/v1/admin/teachers",
        headers=ctx.headers("admin"),
        json={**body, "subject": "other-synthetic"},
    )
    assert conflict.status_code == 409 and conflict.json()["detail"] == "admin_request_conflict"
    duplicate = await ctx.client.post(
        "/api/v1/admin/teachers",
        headers=ctx.headers("admin"),
        json={**body, "request_id": str(uuid4())},
    )
    assert duplicate.status_code == 409
    listing = await ctx.client.get("/api/v1/admin/teachers?limit=1", headers=ctx.headers("admin"))
    assert len(listing.json()["records"]) == 1 and listing.json()["has_next"]
    assert listing.json()["next_offset"] == 1
    all_rows = (await ctx.client.get("/api/v1/admin/teachers", headers=ctx.headers("admin"))).json()
    assert str(ctx.users["admin"].id) not in {r["user_id"] for r in all_rows["records"]}
    assert all(
        set(r) == {"user_id", "role", "is_active", "created_at"} for r in all_rows["records"]
    )
    assert body["subject"] not in str(all_rows)
    audit = await ctx.client.get(
        "/api/v1/admin/audit-events?request_id=" + body["request_id"], headers=ctx.headers("admin")
    )
    rows = audit.json()["records"]
    assert len(rows) == 1 and rows[0]["actor_user_id"] == str(ctx.users["admin"].id)
    assert rows[0]["state"] == {"is_active": True, "is_admin": False}
    assert "subject" not in audit.text and "payload_sha256" not in audit.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "extra",
    [
        {"role": "admin"},
        {"is_admin": True},
        {"actor_user_id": str(uuid4())},
        {"teacher_id": str(uuid4())},
    ],
)
async def test_registration_never_accepts_role_or_actor_input(admin_api, extra):
    ctx = admin_api
    response = await ctx.client.post(
        "/api/v1/admin/teachers", headers=ctx.headers("admin"), json=registration(**extra)
    )
    assert response.status_code == 422
    async with ctx.factory() as session:
        assert await session.scalar(select(func.count()).select_from(AdminOperation)) == 0


@pytest.mark.anyio
async def test_only_configured_exact_issuer_and_valid_identity_can_register(admin_api):
    ctx = admin_api
    for extra in (
        {"issuer": "https://other.invalid"},
        {"subject": " padded "},
        {"subject": "\n"},
        {"subject": ""},
    ):
        response = await ctx.client.post(
            "/api/v1/admin/teachers", headers=ctx.headers("admin"), json=registration(**extra)
        )
        assert response.status_code == 422
    async with ctx.factory() as session:
        assert await session.scalar(select(func.count()).select_from(AdminOperation)) == 0


@pytest.mark.anyio
async def test_disable_reenable_immediate_and_no_admin_account_edit(admin_api):
    ctx = admin_api
    aid = await create(ctx)
    uid = ctx.users["a"].id
    url = f"/api/v1/admin/teachers/{uid}"
    for active in (False, True):
        body = {"request_id": str(uuid4()), "is_active": active}
        response = await ctx.client.patch(url, headers=ctx.headers("admin"), json=body)
        assert response.status_code == 200 and response.json()["state"]["is_active"] == active
        replay = await ctx.client.patch(url, headers=ctx.headers("admin"), json=body)
        assert replay.json() == response.json()
        for path in ("/me", f"/assignments/{aid}"):
            result = await ctx.client.get("/api/v1" + path, headers=ctx.headers())
            assert result.status_code == (200 if active else 403)
    response = await ctx.client.patch(
        f"/api/v1/admin/teachers/{ctx.users['admin'].id}",
        headers=ctx.headers("admin"),
        json={"request_id": str(uuid4()), "is_active": False},
    )
    assert response.status_code == 404
    response = await ctx.client.patch(
        url,
        headers=ctx.headers("admin"),
        json={"request_id": str(uuid4()), "is_active": True, "is_admin": True},
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_membership_grant_revoke_listing_and_audit_same_transaction(admin_api):
    ctx = admin_api
    aid = await create(ctx)
    uid = ctx.users["b"].id
    url = f"/api/v1/assignments/{aid}/members/{uid}"
    rid = str(uuid4())
    body = {"request_id": rid, "can_write": False}
    response = await ctx.client.put(url, headers=ctx.headers("admin"), json=body)
    assert response.status_code == 200
    assert (
        await ctx.client.put(url, headers=ctx.headers("admin"), json=body)
    ).json() == response.json()
    assert (
        await ctx.client.get(f"/api/v1/assignments/{aid}", headers=ctx.headers("b"))
    ).status_code == 200
    listing = await ctx.client.get(
        f"/api/v1/admin/teachers/{uid}/memberships", headers=ctx.headers("admin")
    )
    row = next(r for r in listing.json()["records"] if r["assignment_id"] == aid)
    assert row["is_active"] and not row["can_write"] and row["revoked_at"] is None
    await ctx.client.delete(url + "?request_id=" + str(uuid4()), headers=ctx.headers("admin"))
    assert (
        await ctx.client.get(f"/api/v1/assignments/{aid}", headers=ctx.headers("b"))
    ).status_code == 404
    rows = (
        await ctx.client.get(
            f"/api/v1/admin/teachers/{uid}/memberships", headers=ctx.headers("admin")
        )
    ).json()["records"]
    assert not rows[0]["is_active"] and rows[0]["revoked_at"]
    async with ctx.factory() as session:
        events = list(
            await session.scalars(
                select(AdminOperation).where(AdminOperation.assignment_id == UUID(aid))
            )
        )
        assert len(events) == 2
        assert {e.action for e in events} == {
            "assignment_membership_granted",
            "assignment_membership_revoked",
        }
        assert all(e.actor_user_id == ctx.users["admin"].id for e in events)


@pytest.mark.anyio
async def test_database_role_revocation_invalidates_token_and_stale_service_principal(admin_api):
    ctx = admin_api
    identity = await ctx.verifier.verify(ctx.headers("admin")["Authorization"].split()[1])
    async with ctx.factory() as session:
        principal = await service.resolve_principal(session, identity)
    async with ctx.factory() as session, session.begin():
        await session.execute(
            update(User).where(User.id == principal.user_id).values(is_admin=False)
        )
    me = await ctx.client.get("/api/v1/me", headers=ctx.headers("admin", role="admin"))
    assert me.json()["role"] == "teacher" and "teacher:manage" not in me.json()["capabilities"]
    assert (
        await ctx.client.get("/api/v1/admin/teachers", headers=ctx.headers("admin"))
    ).status_code == 403
    async with ctx.factory() as session:
        with pytest.raises(AccessError, match="administrator_required"):
            await admin_service.activate_teacher(
                session,
                principal,
                ctx.users["b"].id,
                TeacherActivation(request_id=uuid4(), is_active=False),
            )
    async with ctx.factory() as session:
        assert (await session.get(User, ctx.users["b"].id)).is_active


@pytest.mark.anyio
async def test_identity_mapping_cannot_substitute_another_database_user(admin_api):
    ctx = admin_api
    identity = await ctx.verifier.verify(ctx.headers()["Authorization"].split()[1])
    async with ctx.factory() as session:
        principal = await service.resolve_principal(session, identity)
        forged = principal.model_copy(update={"user_id": ctx.users["admin"].id, "is_admin": True})
        with pytest.raises(AccessError, match="teacher_not_provisioned"):
            await service.refresh_principal(session, forged)
    # A signed Token's real subject wins over a forged teacher_id claim.
    me = await ctx.client.get(
        "/api/v1/me", headers=ctx.headers("b", teacher_id=str(ctx.users["admin"].id), role="admin")
    )
    assert me.json()["user_id"] == str(ctx.users["b"].id) and me.json()["role"] == "teacher"


@pytest.mark.anyio
async def test_audit_append_only_and_teachers_cannot_read_or_change_it(admin_api):
    ctx = admin_api
    value, _ = await register(ctx)
    for statement in (
        "UPDATE admin_operations SET state = '{}'::jsonb WHERE id = :id",
        "DELETE FROM admin_operations WHERE id = :id",
    ):
        with pytest.raises(IntegrityError):
            async with ctx.factory() as session, session.begin():
                await session.execute(text(statement), {"id": UUID(value["operation_id"])})
    assert (
        await ctx.client.get("/api/v1/admin/audit-events", headers=ctx.headers())
    ).status_code == 403
    assert (
        await ctx.client.delete("/api/v1/admin/audit-events", headers=ctx.headers())
    ).status_code == 405
    async with ctx.factory() as session:
        event = await session.get(AdminOperation, UUID(value["operation_id"]))
        assert event.state == {"is_active": True, "is_admin": False}


@pytest.mark.anyio
async def test_audit_failure_rolls_back_user_and_safe_database_error(admin_api, monkeypatch):
    ctx = admin_api
    body = registration()

    async def fail(*args, **kwargs):
        raise OperationalError("secret-db-path", {}, Exception("private-config"))

    monkeypatch.setattr(admin_service, "append", fail)
    response = await ctx.client.post(
        "/api/v1/admin/teachers", headers=ctx.headers("admin"), json=body
    )
    assert response.status_code == 503 and response.json() == {
        "detail": "identity_operation_unavailable"
    }
    async with ctx.factory() as session:
        assert await session.scalar(select(User.id).where(User.subject == body["subject"])) is None
        assert await session.scalar(select(func.count()).select_from(AdminOperation)) == 0


@pytest.mark.anyio
async def test_lost_commit_ack_replay_and_concurrent_request_only_one_audit(admin_api, monkeypatch):
    ctx = admin_api
    original = admin_service.activate_teacher
    body = {"request_id": str(uuid4()), "is_active": False}
    url = f"/api/v1/admin/teachers/{ctx.users['b'].id}"

    async def lost_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise OperationalError("commit", {}, Exception("synthetic acknowledgement loss"))

    monkeypatch.setattr(admin_service, "activate_teacher", lost_ack)
    assert (await ctx.client.patch(url, headers=ctx.headers("admin"), json=body)).status_code == 503
    monkeypatch.setattr(admin_service, "activate_teacher", original)
    responses = await asyncio.gather(
        *[ctx.client.patch(url, headers=ctx.headers("admin"), json=body) for _ in range(2)]
    )
    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json() == responses[1].json()
    async with ctx.factory() as session:
        assert not (await session.get(User, ctx.users["b"].id)).is_active
        assert await session.scalar(select(func.count()).select_from(AdminOperation)) == 1


@pytest.mark.anyio
async def test_admin_global_data_compatibility_and_scope_splicing_still_forbidden(admin_api):
    ctx = admin_api
    a, b = await create(ctx), await create(ctx, "b")
    qa, qb = await question(ctx, a), await question(ctx, b, "b")
    for aid in (a, b):
        r = await ctx.client.get(
            f"/api/v1/assignments/{aid}/workbench", headers=ctx.headers("admin")
        )
        assert r.status_code == 200 and r.json()["can_write"]
    assert (
        await ctx.client.get(
            f"/api/v1/assignments/{a}/workbench?question_id={qb}", headers=ctx.headers("admin")
        )
    ).status_code == 404
    assert (
        await ctx.client.get(f"/api/v1/assignments/{b}", headers=ctx.headers())
    ).status_code == 404
    async with ctx.factory() as session:
        assert await session.scalar(select(Question.assignment_id).where(Question.id == UUID(qa)))


@pytest.mark.anyio
async def test_membership_admin_demoted_after_route_check_cannot_write(admin_api, monkeypatch):
    ctx = admin_api
    aid = await create(ctx)
    original = admin_service.change_membership

    async def demote_before_transaction(*args, **kwargs):
        async with ctx.factory() as session, session.begin():
            await session.execute(
                update(User)
                .where(
                    User.id == ctx.users["admin"].id,
                )
                .values(is_admin=False)
            )
        return await original(*args, **kwargs)

    monkeypatch.setattr(admin_service, "change_membership", demote_before_transaction)
    response = await ctx.client.put(
        f"/api/v1/assignments/{aid}/members/{ctx.users['b'].id}",
        headers=ctx.headers("admin"),
        json={"can_write": True},
    )
    assert response.status_code == 403
    async with ctx.factory() as session:
        assert (
            await session.scalar(
                select(AssignmentMembership.id).where(
                    AssignmentMembership.assignment_id == UUID(aid),
                    AssignmentMembership.user_id == ctx.users["b"].id,
                )
            )
            is None
        )
        assert await session.scalar(select(func.count()).select_from(AdminOperation)) == 0
