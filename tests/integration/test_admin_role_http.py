"""Signed JWTs + real isolated PostgreSQL: server roles, administration and audit integrity."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from test_teacher_authorization import auth_api, create, question  # noqa: F401

from huipi_cloud.modules.assignments.models import Question
from huipi_cloud.modules.identity import admin_service, local_admin, service
from huipi_cloud.modules.identity.http_schemas import TeacherActivation
from huipi_cloud.modules.identity.models import (
    AdminOperation,
    AssignmentMembership,
    LocalAuthAudit,
    LocalCredential,
    LocalSession,
    User,
)
from huipi_cloud.modules.identity.protocol import AccessError


@pytest.fixture
async def admin_api(auth_api, monkeypatch):  # noqa: F811
    ctx = auth_api
    ctx.registered = []
    yield ctx
    async with ctx.factory() as session, session.begin():
        await session.execute(delete(LocalSession).where(LocalSession.user_id.in_(ctx.registered)))
        await session.execute(
            delete(LocalCredential).where(LocalCredential.user_id.in_(ctx.registered))
        )
        await session.execute(text("TRUNCATE TABLE local_auth_audit"))
        await session.execute(delete(User).where(User.id.in_(ctx.registered)))


def registration(**extra):
    return {
        "request_id": str(uuid4()),
        "account": "synthetic-" + uuid4().hex + "@example.invalid",
        "display_name": "合成教师",
        "password": "synthetic-secure-password-2026!",
        **extra,
    }


async def register(ctx, payload=None):
    body = payload or registration()
    response = await ctx.client.post(
        "/api/v1/admin/local-accounts", headers=ctx.headers("admin"), json=body
    )
    assert response.status_code == 201, response.text
    uid = UUID(response.json()["user_id"])
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
        ("POST", "/admin/local-accounts", registration()),
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
        "/api/v1/admin/local-accounts",
        headers=ctx.headers("admin"),
        json={**body, "account": "other-synthetic@example.invalid"},
    )
    assert conflict.status_code == 409 and conflict.json()["detail"] == "account_request_conflict"
    duplicate = await ctx.client.post(
        "/api/v1/admin/local-accounts",
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
    assert body["account"] not in str(all_rows)
    audit = await ctx.client.get("/api/v1/admin/local-accounts/audit", headers=ctx.headers("admin"))
    rows = audit.json()["records"]
    row = next(r for r in rows if r["request_id"] == body["request_id"])
    assert row["actor_user_id"] == str(ctx.users["admin"].id)
    assert body["account"] not in audit.text and "payload_sha256" not in audit.text


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
        "/api/v1/admin/local-accounts", headers=ctx.headers("admin"), json=registration(**extra)
    )
    assert response.status_code == 422
    async with ctx.factory() as session:
        assert await session.scalar(select(func.count()).select_from(AdminOperation)) == 0


@pytest.mark.anyio
async def test_only_valid_local_account_and_password_can_register(admin_api):
    ctx = admin_api
    for extra in (
        {"account": "not-a-valid-account"},
        {"account": ""},
        {"password": "short"},
        {"display_name": ""},
    ):
        response = await ctx.client.post(
            "/api/v1/admin/local-accounts", headers=ctx.headers("admin"), json=registration(**extra)
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
    value, body = await register(ctx)
    action = await ctx.client.patch(
        f"/api/v1/admin/teachers/{ctx.users['b'].id}",
        headers=ctx.headers("admin"),
        json={"request_id": str(uuid4()), "is_active": False},
    )
    assert action.status_code == 200
    async with ctx.factory() as session:
        local_event = await session.scalar(
            select(LocalAuthAudit).where(LocalAuthAudit.request_id == UUID(body["request_id"]))
        )
        local_event_id = local_event.id
    for statement, event_id in (
        (
            "UPDATE admin_operations SET state = '{}'::jsonb WHERE id = :id",
            UUID(action.json()["operation_id"]),
        ),
        ("DELETE FROM admin_operations WHERE id = :id", UUID(action.json()["operation_id"])),
        ("UPDATE local_auth_audit SET action = 'tampered' WHERE id = :id", local_event_id),
        ("DELETE FROM local_auth_audit WHERE id = :id", local_event_id),
    ):
        with pytest.raises(IntegrityError):
            async with ctx.factory() as session, session.begin():
                await session.execute(text(statement), {"id": event_id})
    assert (
        await ctx.client.get("/api/v1/admin/audit-events", headers=ctx.headers())
    ).status_code == 403
    assert (
        await ctx.client.delete("/api/v1/admin/audit-events", headers=ctx.headers())
    ).status_code == 405
    assert (
        await ctx.client.get("/api/v1/admin/local-accounts/audit", headers=ctx.headers())
    ).status_code == 403
    async with ctx.factory() as session:
        event = await session.get(LocalAuthAudit, local_event_id)
        assert event.action == "local_account_created" and event.target_user_id == UUID(
            value["user_id"]
        )


@pytest.mark.anyio
async def test_audit_failure_rolls_back_user_and_safe_database_error(admin_api, monkeypatch):
    ctx = admin_api
    body = registration()

    async def fail(*args, **kwargs):
        raise OperationalError("secret-db-path", {}, Exception("private-config"))

    monkeypatch.setattr(local_admin, "_append_audit", fail)
    response = await ctx.client.post(
        "/api/v1/admin/local-accounts", headers=ctx.headers("admin"), json=body
    )
    assert response.status_code == 503 and response.json() == {
        "detail": "identity_operation_unavailable"
    }
    async with ctx.factory() as session:
        assert (
            await session.scalar(
                select(LocalCredential.user_id).where(LocalCredential.login_name == body["account"])
            )
            is None
        )
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


@pytest.mark.anyio
async def test_cached_orm_user_cannot_keep_revoked_administrator_authority(admin_api):
    ctx = admin_api
    identity = await ctx.verifier.verify(ctx.headers("admin")["Authorization"].split()[1])
    async with ctx.factory() as cached_session:
        async with cached_session.begin():
            cached_user = await cached_session.get(User, ctx.users["admin"].id)
            principal = await service.resolve_principal(cached_session, identity)
        assert cached_user.is_admin
        async with ctx.factory() as other, other.begin():
            await other.execute(
                update(User)
                .where(
                    User.id == cached_user.id,
                )
                .values(is_admin=False)
            )
        # Keep a strong reference: SQLAlchemy's identity map must not reuse stale authority.
        with pytest.raises(AccessError, match="administrator_required"):
            await admin_service.activate_teacher(
                cached_session,
                principal,
                ctx.users["b"].id,
                TeacherActivation(request_id=uuid4(), is_active=False),
            )
    async with ctx.factory() as session:
        assert (await session.get(User, ctx.users["b"].id)).is_active
        assert await session.scalar(select(func.count()).select_from(AdminOperation)) == 0
