"""Password hashing and revocable, opaque local sessions.

Only password hashes and SHA-256 digests of random session/CSRF tokens are stored.
No external identity provider or client-supplied role participates in authentication.
"""

import asyncio
import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import case, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.auth.jwt import VerifiedIdentity
from huipi_cloud.infrastructure.database import session as database
from huipi_cloud.modules.identity.models import (
    LocalAuthAudit,
    LocalAuthThrottle,
    LocalCredential,
    LocalSession,
    User,
)
from huipi_cloud.modules.identity.protocol import AccessError

SESSION_COOKIE = "huipi_session" if settings.app_env == "development" else "__Host-huipi_session"
CSRF_COOKIE = "huipi_csrf" if settings.app_env == "development" else "__Host-huipi_csrf"
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_EMAIL = re.compile(r"^[^@\s]{1,100}@[^@\s.]+(?:\.[^@\s.]+)+$")
_PHONE = re.compile(r"^\+?[0-9]{6,20}$")
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)
_dummy_hash = _hasher.hash("This-is-a-synthetic-dummy-password-7")
_hash_slots = asyncio.Semaphore(4)


def normalize_login(value: str) -> str:
    value = value.strip().casefold()
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("请输入有效邮箱或手机号") from None
    if len(value) > 254 or not (_EMAIL.fullmatch(value) or _PHONE.fullmatch(value)):
        raise ValueError("请输入有效邮箱或手机号")
    return value


def validate_password(value: str) -> None:
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeError:
        raise ValueError("密码包含无效字符") from None
    if not 6 <= len(value) <= 128 or encoded_length > 512:
        raise ValueError("密码须为6至128个字符")


async def hash_password(value: str) -> str:
    validate_password(value)
    async with _hash_slots:
        return await asyncio.to_thread(_hasher.hash, value)


async def verify_password(encoded: str, value: str) -> bool:
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeError:
        encoded_length = 513
    invalid = len(value) > 128 or encoded_length > 512
    if invalid:
        value = "invalid-input-too-long"
    async with _hash_slots:
        try:
            matched = await asyncio.to_thread(_hasher.verify, encoded, value)
            return matched and not invalid
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _valid_token(token: str | None) -> bool:
    return bool(token and _TOKEN.fullmatch(token))


async def _throttle(session, kind: str, value: str, maximum: int) -> bool:
    """An atomic, database-backed fixed window shared by all API processes."""
    key = hashlib.sha256((kind + ":" + value).encode()).hexdigest()
    statement = insert(LocalAuthThrottle).values(
        key_hash=key, attempts=1, window_started_at=func.clock_timestamp()
    )
    expired = LocalAuthThrottle.window_started_at < func.clock_timestamp() - text(
        "interval '15 minutes'"
    )
    statement = statement.on_conflict_do_update(
        index_elements=[LocalAuthThrottle.key_hash],
        set_={
            "attempts": case((expired, 1), else_=LocalAuthThrottle.attempts + 1),
            "window_started_at": case(
                (expired, func.clock_timestamp()), else_=LocalAuthThrottle.window_started_at
            ),
        },
    ).returning(LocalAuthThrottle.attempts)
    return int(await session.scalar(statement)) <= maximum


async def login(session, login_name: str, password: str, peer: str, *, hours: int):
    try:
        normalized = normalize_login(login_name)
    except ValueError:
        # Invalid and unknown accounts have the same response and hashing path.
        normalized = "invalid:" + hashlib.sha256(login_name.encode("utf-8", "replace")).hexdigest()
    async with session.begin():
        allowed_ip = await _throttle(session, "login-ip", peer, 60)
        allowed_account = (
            await _throttle(session, "login-account", normalized, 10) if allowed_ip else False
        )
    if not allowed_ip or not allowed_account:
        raise AccessError("login_rate_limited", 429)
    async with session.begin():
        row = (
            await session.execute(
                select(LocalCredential, User)
                .join(User, User.id == LocalCredential.user_id)
                .where(LocalCredential.login_name == normalized)
            )
        ).first()
        encoded = row[0].password_hash if row and row[1].is_active else _dummy_hash
        user_id = row[1].id if row else None
    valid = await verify_password(encoded, password)
    if not valid or user_id is None:
        raise AccessError("invalid_credentials", 401)
    async with session.begin():
        # A password reset or account suspension during hashing must win.
        row = (
            await session.execute(
                select(LocalCredential, User)
                .join(User, User.id == LocalCredential.user_id)
                .where(User.id == user_id, User.is_active.is_(True))
                .with_for_update(of=User)
                .execution_options(populate_existing=True)
            )
        ).first()
        if not row or row[0].password_hash != encoded:
            raise AccessError("invalid_credentials", 401)
        credential, user = row
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(hours=hours)
        session.add(
            LocalSession(
                token_hash=token_hash(token),
                csrf_hash=token_hash(csrf),
                user_id=user.id,
                expires_at=expires,
            )
        )
        session.add(LocalAuthAudit(actor_user_id=user.id, target_user_id=user.id, action="login"))
        await session.execute(
            delete(LocalAuthThrottle).where(
                LocalAuthThrottle.key_hash
                == hashlib.sha256(("login-account:" + normalized).encode()).hexdigest()
            )
        )
        return (
            token,
            csrf,
            user.id,
            credential.display_name,
            credential.must_change_password,
            credential.recovery_email,
        )


class LocalSessionVerifier:
    """Resolve only random local session tokens; old OIDC JWTs are not accepted."""

    def __init__(self, _config=None):
        pass

    async def verify(self, token: str, *, csrf: str | None = None, require_csrf: bool = False):
        if not _valid_token(token):
            raise AccessError("invalid_session", 401)
        if require_csrf and not _valid_token(csrf):
            raise AccessError("csrf_failed", 403)
        factory = database.session_factory
        if factory is None:
            raise AccessError("authorization_unavailable", 503)
        try:
            async with asyncio.timeout(5), factory() as session:
                row = (
                    await session.execute(
                        select(User, LocalCredential, LocalSession)
                        .join(LocalCredential, LocalCredential.user_id == User.id)
                        .join(LocalSession, LocalSession.user_id == User.id)
                        .where(
                            LocalSession.token_hash == token_hash(token),
                            LocalSession.revoked_at.is_(None),
                            LocalSession.expires_at > func.now(),
                            User.is_active.is_(True),
                        )
                    )
                ).first()
                if row is None:
                    raise AccessError("invalid_session", 401)
                user, credential, local_session = row
                if require_csrf and not hmac.compare_digest(
                    local_session.csrf_hash, token_hash(csrf)
                ):
                    raise AccessError("csrf_failed", 403)
                return VerifiedIdentity(
                    user.issuer,
                    user.subject,
                    local_session.expires_at,
                    credential.must_change_password,
                    local_session.token_hash,
                )
        except (SQLAlchemyError, OSError, TimeoutError):
            raise AccessError("authorization_unavailable", 503) from None
