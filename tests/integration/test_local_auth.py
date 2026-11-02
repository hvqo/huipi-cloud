"""Real isolated PostgreSQL tests for local credentials and browser sessions."""

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from huipi_cloud.infrastructure.database import session as database
from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.main import app
from huipi_cloud.modules.identity import dependencies, local_auth, mail
from huipi_cloud.modules.identity.models import (
    LocalAuthAudit,
    LocalAuthThrottle,
    LocalCredential,
    LocalPasswordReset,
    LocalSession,
    User,
)

ORIGIN = "http://127.0.0.1:5173"
HEADERS = {"Origin": ORIGIN, "X-Huipi-Client": "1"}
PASSWORD = "synthetic-password-2026!"
NEW_PASSWORD = "different-synthetic-password-2026!"


@pytest.fixture
async def local_accounts(postgres_engine, monkeypatch):
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    monkeypatch.setattr(database, "session_factory", factory)
    monkeypatch.setattr(dependencies, "verifier", local_auth.LocalSessionVerifier())
    old = app.dependency_overrides.copy()

    async def db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[dependencies.get_auth_session] = db
    app.dependency_overrides.pop(dependencies.get_current_principal, None)
    app.dependency_overrides.pop(dependencies.authorize_request, None)
    suffix = uuid4().hex
    ids = []
    accounts = {}
    encoded = await local_auth.hash_password(PASSWORD)
    async with factory() as session, session.begin():
        for name, is_admin in [("teacher", False), ("admin", True), ("other", False)]:
            user = User(
                issuer="https://historical.invalid",
                subject=str(uuid4()),
                is_admin=is_admin,
                is_active=True,
            )
            session.add(user)
            await session.flush()
            login = f"{name}-{suffix}@example.invalid"
            session.add(
                LocalCredential(
                    user_id=user.id,
                    login_name=login,
                    display_name=name,
                    password_hash=encoded,
                    must_change_password=False,
                )
            )
            ids.append(user.id)
            accounts[name] = (login, user.id)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50001))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        yield client, factory, accounts, ids
    app.dependency_overrides.clear()
    app.dependency_overrides.update(old)
    async with factory() as session, session.begin():
        await session.execute(delete(LocalSession).where(LocalSession.user_id.in_(ids)))
        await session.execute(delete(LocalPasswordReset).where(LocalPasswordReset.user_id.in_(ids)))
        await session.execute(delete(LocalCredential).where(LocalCredential.user_id.in_(ids)))
        await session.execute(text("TRUNCATE TABLE local_auth_audit"))
        await session.execute(delete(User).where(User.id.in_(ids)))
        await session.execute(delete(LocalAuthThrottle))


async def sign_in(client, account, password=PASSWORD):
    return await client.post(
        "/api/v1/auth/login", headers=HEADERS, json={"account": account, "password": password}
    )


def csrf(client):
    return {**HEADERS, "X-Huipi-CSRF": client.cookies.get("huipi_csrf", "")}


@pytest.mark.anyio
async def test_local_login_session_csrf_and_old_token_rejected(local_accounts):
    client, factory, accounts, ids = local_accounts
    invalid = await sign_in(client, accounts["teacher"][0], "incorrect-password")
    assert invalid.status_code == 401
    good = await sign_in(client, accounts["teacher"][0].upper())
    assert good.status_code == 200
    assert PASSWORD not in good.text and "token" not in good.text
    cookies = good.headers.get_list("set-cookie")
    assert any(
        "huipi_session=" in item and "HttpOnly" in item and "SameSite=strict" in item
        for item in cookies
    )
    assert (await client.get("/api/v1/me")).json()["role"] == "teacher"
    assert (await client.post("/api/v1/assignments", headers=HEADERS, json={})).status_code == 403
    assert (
        await client.post(
            "/api/v1/assignments",
            headers={**csrf(client), "Origin": "https://evil.invalid"},
            json={},
        )
    ).status_code == 403
    assert (await client.get("/api/v1/admin/local-accounts")).status_code == 403
    logout = await client.post("/api/v1/auth/logout", headers=csrf(client), json={})
    assert logout.status_code == 204
    assert (await client.get("/api/v1/me")).status_code == 401
    assert (
        await client.get("/api/v1/me", headers={"Authorization": "Bearer fake.oidc.jwt"})
    ).status_code == 401
    async with factory() as db:
        credential = await db.get(LocalCredential, ids[0])
        assert (
            credential.password_hash.startswith("$argon2id$")
            and PASSWORD not in credential.password_hash
        )
        sessions = (
            await db.scalars(select(LocalSession).where(LocalSession.user_id == ids[0]))
        ).all()
        assert sessions and all(
            s.revoked_at is not None and len(s.token_hash) == 64 for s in sessions
        )
    with pytest.raises(IntegrityError):
        async with factory() as db, db.begin():
            await db.execute(text("UPDATE local_auth_audit SET action = 'tampered'"))


@pytest.mark.anyio
async def test_admin_create_reset_and_mandatory_password_change(local_accounts):
    client, factory, accounts, ids = local_accounts
    await sign_in(client, accounts["admin"][0])
    account = f"new-{uuid4().hex}@example.invalid"
    request_id = str(uuid4())
    payload = {
        "request_id": request_id,
        "account": account,
        "display_name": "合成教师",
        "password": PASSWORD,
    }
    created = await client.post("/api/v1/admin/local-accounts", headers=csrf(client), json=payload)
    assert created.status_code == 201, created.text
    new_id = created.json()["user_id"]
    ids.append(__import__("uuid").UUID(new_id))
    replay = await client.post("/api/v1/admin/local-accounts", headers=csrf(client), json=payload)
    assert replay.json() == created.json()
    conflict = await client.post(
        "/api/v1/admin/local-accounts",
        headers=csrf(client),
        json={**payload, "password": NEW_PASSWORD},
    )
    assert conflict.status_code == 409
    await client.post("/api/v1/auth/logout", headers=csrf(client), json={})
    assert (await sign_in(client, account)).json()["must_change_password"] is True
    assert (await client.get("/api/v1/assignments")).status_code == 403
    change = await client.post(
        "/api/v1/auth/change-password",
        headers=csrf(client),
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert change.status_code == 204
    assert (await client.get("/api/v1/me")).status_code == 401
    assert (await sign_in(client, account, NEW_PASSWORD)).status_code == 200
    assert (await client.get("/api/v1/me")).status_code == 200
    assert (
        await client.post(
            f"/api/v1/admin/local-accounts/{ids[0]}/reset-password",
            headers=csrf(client),
            json={"request_id": str(uuid4()), "password": PASSWORD},
        )
    ).status_code == 403


@pytest.mark.anyio
async def test_verified_email_reset_is_single_use_and_revokes_sessions(local_accounts, monkeypatch):
    client, factory, accounts, ids = local_accounts
    delivered = []
    monkeypatch.setattr(mail, "configured", lambda: True)

    async def send(email, token, purpose):
        delivered.append((email, token, purpose))

    monkeypatch.setattr(mail, "send", send)
    await sign_in(client, accounts["teacher"][0])
    result = await client.post(
        "/api/v1/auth/recovery-email/request",
        headers=csrf(client),
        json={"email": "teacher-recovery@example.invalid"},
    )
    assert result.status_code == 200
    verification = delivered[-1][1]
    assert (
        await client.post(
            "/api/v1/auth/recovery-email/confirm",
            headers=csrf(client),
            json={"token": verification},
        )
    ).status_code == 204
    assert (
        await client.post(
            "/api/v1/auth/recovery-email/confirm",
            headers=csrf(client),
            json={"token": verification},
        )
    ).status_code == 400
    unknown = await client.post(
        "/api/v1/auth/reset/request",
        headers=HEADERS,
        json={"account": "not-present@example.invalid"},
    )
    reset = await client.post(
        "/api/v1/auth/reset/request", headers=HEADERS, json={"account": accounts["teacher"][0]}
    )
    assert unknown.json() == reset.json()
    reset_token = delivered[-1][1]
    assert delivered[-1][2] == "reset_password"
    async with factory() as db:
        stored = await db.scalar(
            select(LocalPasswordReset).where(
                LocalPasswordReset.token_hash == local_auth.token_hash(reset_token)
            )
        )
        assert stored and reset_token != stored.token_hash

    async def confirm():
        return await client.post(
            "/api/v1/auth/reset/confirm",
            headers=HEADERS,
            json={"token": reset_token, "password": NEW_PASSWORD},
        )

    results = await asyncio.gather(confirm(), confirm())
    assert sorted(r.status_code for r in results) == [204, 400]
    assert (await client.get("/api/v1/me")).status_code == 401
    assert (await sign_in(client, accounts["teacher"][0], NEW_PASSWORD)).status_code == 200


@pytest.mark.anyio
async def test_admin_reset_and_disable_revoke_existing_sessions(local_accounts):
    client, factory, accounts, ids = local_accounts
    assert (await sign_in(client, accounts["teacher"][0])).status_code == 200
    old_cookie = client.cookies.get("huipi_session")
    assert (await sign_in(client, accounts["admin"][0])).status_code == 200
    body = {"request_id": str(uuid4()), "password": NEW_PASSWORD}
    result = await client.post(
        f"/api/v1/admin/local-accounts/{ids[0]}/reset-password", headers=csrf(client), json=body
    )
    assert result.status_code == 200, result.text
    assert (
        await client.post(
            f"/api/v1/admin/local-accounts/{ids[0]}/reset-password", headers=csrf(client), json=body
        )
    ).json() == result.json()
    assert (
        await client.get("/api/v1/me", headers={"Authorization": f"Bearer {old_cookie}"})
    ).status_code == 401
    assert (await sign_in(client, accounts["teacher"][0], NEW_PASSWORD)).json()[
        "must_change_password"
    ]
    assert (await client.get("/api/v1/me")).status_code == 403
    await sign_in(client, accounts["admin"][0])
    disabled = await client.patch(
        f"/api/v1/admin/teachers/{ids[0]}",
        headers=csrf(client),
        json={"request_id": str(uuid4()), "is_active": False},
    )
    assert disabled.status_code == 200
    assert (await sign_in(client, accounts["teacher"][0], NEW_PASSWORD)).status_code == 401
    enabled = await client.patch(
        f"/api/v1/admin/teachers/{ids[0]}",
        headers=csrf(client),
        json={"request_id": str(uuid4()), "is_active": True},
    )
    assert enabled.status_code == 200
    assert (
        await client.get("/api/v1/me", headers={"Authorization": f"Bearer {old_cookie}"})
    ).status_code == 401
    async with factory() as db:
        user = await db.get(User, ids[0])
        assert user.issuer == "https://historical.invalid"
        audits = (
            await db.scalars(select(LocalAuthAudit).where(LocalAuthAudit.target_user_id == ids[0]))
        ).all()
        assert any(a.action == "local_password_reset_by_admin" for a in audits)
        assert all(NEW_PASSWORD not in (a.payload_sha256 or "") for a in audits)


@pytest.mark.anyio
async def test_login_rate_limit_and_secret_validation_are_safe(local_accounts):
    client, _, accounts, _ = local_accounts
    bad = await client.post(
        "/api/v1/auth/login",
        headers=HEADERS,
        json={"account": accounts["teacher"][0], "password": "x" * 129},
    )
    assert bad.status_code == 422 and "x" * 129 not in bad.text
    cross_origin = await client.post(
        "/api/v1/auth/login",
        headers={**HEADERS, "Origin": "https://evil.invalid"},
        json={"account": accounts["teacher"][0], "password": PASSWORD},
    )
    assert cross_origin.status_code == 403
    for _ in range(10):
        assert (await sign_in(client, accounts["teacher"][0], "incorrect")).status_code == 401
    assert (await sign_in(client, accounts["teacher"][0], PASSWORD)).status_code == 429


@pytest.mark.anyio
async def test_blocked_ip_does_not_create_throttle_rows_for_unlimited_accounts(local_accounts):
    client, factory, _, _ = local_accounts
    async with factory() as db, db.begin():
        for kind, attempts in [("login-ip", 60), ("reset-ip", 20)]:
            db.add(
                LocalAuthThrottle(
                    key_hash=hashlib.sha256(f"{kind}:127.0.0.1".encode()).hexdigest(),
                    attempts=attempts,
                    window_started_at=datetime.now(UTC),
                )
            )
    for index in range(5):
        account = f"blocked-{index}@example.invalid"
        assert (await sign_in(client, account, "incorrect")).status_code == 429
        result = await client.post(
            "/api/v1/auth/reset/request", headers=HEADERS, json={"account": account}
        )
        assert result.status_code == 200
    async with factory() as db:
        assert len((await db.scalars(select(LocalAuthThrottle))).all()) == 2


@pytest.mark.anyio
async def test_no_mail_configuration_does_not_fake_email_delivery(local_accounts, monkeypatch):
    client, _, accounts, _ = local_accounts
    monkeypatch.setattr(mail, "configured", lambda: False)
    await sign_in(client, accounts["teacher"][0])
    result = await client.post(
        "/api/v1/auth/recovery-email/request",
        headers=csrf(client),
        json={"email": "teacher@example.invalid"},
    )
    assert result.status_code == 503
    unknown = await client.post(
        "/api/v1/auth/reset/request", headers=HEADERS, json={"account": "unknown@example.invalid"}
    )
    known = await client.post(
        "/api/v1/auth/reset/request", headers=HEADERS, json={"account": accounts["teacher"][0]}
    )
    assert unknown.status_code == known.status_code == 200 and unknown.json() == known.json()


@pytest.mark.anyio
async def test_explicit_binding_preserves_historical_user_and_grants(local_accounts):
    client, factory, accounts, ids = local_accounts
    legacy_id = ids[2]
    async with factory() as db, db.begin():
        legacy = await db.get(User, legacy_id)
        prior_identity = (legacy.id, legacy.issuer, legacy.subject)
        await db.execute(delete(LocalCredential).where(LocalCredential.user_id == legacy_id))
    await sign_in(client, accounts["admin"][0])
    new_account = f"bound-{uuid4().hex}@example.invalid"
    body = {
        "request_id": str(uuid4()),
        "account": new_account,
        "display_name": "历史教师",
        "password": NEW_PASSWORD,
    }
    result = await client.post(
        f"/api/v1/admin/local-accounts/{legacy_id}/bind", headers=csrf(client), json=body
    )
    assert result.status_code == 200 and result.json()["user_id"] == str(legacy_id)
    assert (
        await client.post(
            f"/api/v1/admin/local-accounts/{legacy_id}/bind", headers=csrf(client), json=body
        )
    ).json() == result.json()
    duplicate = await client.post(
        f"/api/v1/admin/local-accounts/{legacy_id}/bind",
        headers=csrf(client),
        json={**body, "request_id": str(uuid4())},
    )
    assert duplicate.status_code == 409
    async with factory() as db:
        legacy = await db.get(User, legacy_id)
        assert (legacy.id, legacy.issuer, legacy.subject) == prior_identity
    await client.post("/api/v1/auth/logout", headers=csrf(client), json={})
    signed = await sign_in(client, new_account, NEW_PASSWORD)
    assert signed.status_code == 200 and signed.json()["user_id"] == str(legacy_id)
    assert signed.json()["must_change_password"] is True


@pytest.mark.anyio
async def test_bootstrap_can_attach_existing_admin_once_without_changing_identity(
    postgres_engine, monkeypatch
):
    from argparse import Namespace

    from huipi_cloud.modules.identity import __main__ as cli

    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    monkeypatch.setattr(cli, "session_factory", factory)

    async def no_dispose():
        pass

    monkeypatch.setattr(cli, "dispose_database_engine", no_dispose)
    async with factory() as db, db.begin():
        admin = User(
            issuer="https://old.example.invalid",
            subject=str(uuid4()),
            is_admin=True,
            is_active=True,
        )
        other_admin = User(
            issuer="https://old.example.invalid",
            subject=str(uuid4()),
            is_admin=True,
            is_active=True,
        )
        db.add_all([admin, other_admin])
        await db.flush()
        admin_id, subject = admin.id, admin.subject
        other_admin_id = other_admin.id
    args = Namespace(
        command="bootstrap-local-admin",
        confirm_local_admin_bootstrap=True,
        existing_user_id=admin_id,
        account=f"admin-{uuid4().hex}@example.invalid",
        display_name="合成管理员",
        password="123456",
    )
    try:
        await cli.run(args)
        async with factory() as db:
            saved = await db.get(User, admin_id)
            credential = await db.get(LocalCredential, admin_id)
            assert (saved.issuer, saved.subject, saved.is_admin) == (
                "https://old.example.invalid",
                subject,
                True,
            )
            assert credential is not None and await local_auth.verify_password(
                credential.password_hash, "123456"
            )
        with pytest.raises(SystemExit, match="已有本地登录凭据"):
            await cli.run(args)
        args.existing_user_id = other_admin_id
        args.account = f"other-admin-{uuid4().hex}@example.invalid"
        await cli.run(args)
        async with factory() as db:
            other = await db.get(LocalCredential, other_admin_id)
            assert other is not None and await local_auth.verify_password(
                other.password_hash, "123456"
            )
    finally:
        async with factory() as db, db.begin():
            await db.execute(
                delete(LocalCredential).where(
                    LocalCredential.user_id.in_([admin_id, other_admin_id])
                )
            )
            await db.execute(text("TRUNCATE TABLE local_auth_audit"))
            await db.execute(delete(User).where(User.id.in_([admin_id, other_admin_id])))


@pytest.mark.anyio
async def test_operator_can_recover_only_existing_admin_and_revoke_old_sessions(
    local_accounts, monkeypatch
):
    from argparse import Namespace
    from datetime import UTC, datetime, timedelta

    from huipi_cloud.modules.identity import __main__ as cli

    client, factory, accounts, _ = local_accounts
    monkeypatch.setattr(cli, "session_factory", factory)

    async def no_dispose():
        pass

    monkeypatch.setattr(cli, "dispose_database_engine", no_dispose)
    assert (await sign_in(client, accounts["admin"][0])).status_code == 200
    token = "a" * 43
    async with factory() as db, db.begin():
        db.add(
            LocalPasswordReset(
                user_id=accounts["admin"][1],
                token_hash=local_auth.token_hash(token),
                purpose="reset_password",
                target_email="synthetic@example.invalid",
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
    args = Namespace(
        command="reset-local-admin-password",
        user_id=accounts["admin"][1],
        confirm_local_admin_password_reset=False,
        password="654321",
    )
    with pytest.raises(SystemExit, match="明确确认"):
        await cli.run(args)
    args.confirm_local_admin_password_reset = True
    args.user_id = accounts["teacher"][1]
    with pytest.raises(SystemExit, match="启用管理员"):
        await cli.run(args)
    args.user_id = accounts["admin"][1]
    await cli.run(args)
    assert (await client.get("/api/v1/auth/session")).status_code == 401
    assert (await sign_in(client, accounts["admin"][0], PASSWORD)).status_code == 401
    signed = await sign_in(client, accounts["admin"][0], "654321")
    assert signed.status_code == 200 and signed.json()["must_change_password"] is True
    async with factory() as db:
        item = await db.scalar(
            select(LocalPasswordReset).where(
                LocalPasswordReset.token_hash == local_auth.token_hash(token)
            )
        )
        audit = await db.scalar(
            select(LocalAuthAudit).where(LocalAuthAudit.action == "local_admin_reset_by_operator")
        )
        assert item.used_at is not None
        assert audit.target_user_id == accounts["admin"][1] and audit.actor_user_id is None


@pytest.mark.anyio
async def test_phone_login_can_bind_a_verified_recovery_email(local_accounts, monkeypatch):
    client, factory, accounts, ids = local_accounts
    phone = "+8613800138000"
    async with factory() as db, db.begin():
        credential = await db.get(LocalCredential, ids[0])
        credential.login_name = phone
    delivered = []
    monkeypatch.setattr(mail, "configured", lambda: True)

    async def send(email, token, purpose):
        delivered.append((email, token, purpose))

    monkeypatch.setattr(mail, "send", send)
    assert (await sign_in(client, phone)).status_code == 200
    assert (
        await client.post(
            "/api/v1/auth/recovery-email/request",
            headers=csrf(client),
            json={"email": "phone-recovery@example.invalid"},
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/v1/auth/recovery-email/confirm",
            headers=csrf(client),
            json={"token": delivered[-1][1]},
        )
    ).status_code == 204
    await client.post("/api/v1/auth/logout", headers=csrf(client), json={})
    assert (
        await client.post("/api/v1/auth/reset/request", headers=HEADERS, json={"account": phone})
    ).status_code == 200
    assert delivered[-1][0] == "phone-recovery@example.invalid"
    assert delivered[-1][2] == "reset_password"
    assert (
        await client.post(
            "/api/v1/auth/reset/confirm",
            headers=HEADERS,
            json={"token": delivered[-1][1], "password": NEW_PASSWORD},
        )
    ).status_code == 204
    assert (await sign_in(client, phone, NEW_PASSWORD)).status_code == 200


@pytest.mark.anyio
async def test_password_reset_during_login_cannot_issue_session_from_old_password(
    local_accounts, monkeypatch
):
    client, factory, accounts, ids = local_accounts
    assert (await sign_in(client, accounts["admin"][0])).status_code == 200
    ready, resume = asyncio.Event(), asyncio.Event()
    original = local_auth.verify_password

    async def delayed_verify(encoded, password):
        valid = await original(encoded, password)
        if password == PASSWORD:
            ready.set()
            await resume.wait()
        return valid

    monkeypatch.setattr(local_auth, "verify_password", delayed_verify)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50002))
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as other:
        pending = asyncio.create_task(sign_in(other, accounts["teacher"][0]))
        await asyncio.wait_for(ready.wait(), timeout=5)
        reset = await client.post(
            f"/api/v1/admin/local-accounts/{ids[0]}/reset-password",
            headers=csrf(client),
            json={"request_id": str(uuid4()), "password": NEW_PASSWORD},
        )
        assert reset.status_code == 200
        resume.set()
        assert (await asyncio.wait_for(pending, timeout=5)).status_code == 401
        assert (await other.get("/api/v1/me")).status_code == 401
    async with factory() as db:
        assert not (
            await db.scalars(select(LocalSession).where(LocalSession.user_id == ids[0]))
        ).all()


@pytest.mark.anyio
async def test_self_service_endpoints_require_real_session(local_accounts):
    client, _, _, _ = local_accounts
    assert (await client.get("/api/v1/auth/session")).status_code == 401
    cases = [
        ("/api/v1/auth/logout", {}),
        (
            "/api/v1/auth/change-password",
            {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        ),
        ("/api/v1/auth/recovery-email/request", {"email": "t@example.invalid"}),
        ("/api/v1/auth/recovery-email/confirm", {"token": "a" * 43}),
    ]
    for path, body in cases:
        assert (await client.post(path, headers=HEADERS, json=body)).status_code == 401


@pytest.mark.anyio
async def test_production_cookie_is_secure_and_http_login_origin_rejected(
    local_accounts, monkeypatch
):
    client, _, accounts, _ = local_accounts
    from huipi_cloud.core.config import settings

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(
        settings, "auth_allowed_origins", "http://example.invalid,https://example.invalid"
    )
    blocked = await client.post(
        "/api/v1/auth/login",
        headers={**HEADERS, "Origin": "http://example.invalid"},
        json={"account": accounts["teacher"][0], "password": PASSWORD},
    )
    assert blocked.status_code == 403
    result = await client.post(
        "https://example.invalid/api/v1/auth/login",
        headers={**HEADERS, "Origin": "https://example.invalid"},
        json={"account": accounts["teacher"][0], "password": PASSWORD},
    )
    assert result.status_code == 200
    assert any(
        "huipi_session=" in cookie
        and "Secure" in cookie
        and "HttpOnly" in cookie
        and "SameSite=strict" in cookie
        for cookie in result.headers.get_list("set-cookie")
    )
