"""Local account login and session endpoints; no external identity provider."""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.modules.identity import local_auth, mail
from huipi_cloud.modules.identity.models import (
    LocalAuthAudit,
    LocalCredential,
    LocalPasswordReset,
    LocalSession,
    User,
)
from huipi_cloud.modules.identity.protocol import AccessError

router = APIRouter(prefix="/auth", tags=["local-authentication"])
Db = Annotated[AsyncSession, Depends(get_db_session)]
logger = logging.getLogger(__name__)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128, repr=False)


class PasswordChangeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=128, repr=False)
    new_password: str = Field(min_length=6, max_length=128, repr=False)


class SessionRead(BaseModel):
    user_id: str
    display_name: str | None
    must_change_password: bool
    recovery_email: str | None = None


class EmailBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)


class TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=43, max_length=43, repr=False)


class ResetRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str = Field(min_length=1, max_length=254)


class ResetPasswordBody(TokenBody):
    password: str = Field(min_length=6, max_length=128, repr=False)


class MessageRead(BaseModel):
    message: str


def check_origin(request: Request, *, required: bool) -> None:
    origin = request.headers.get("origin")
    allowed = {item.strip() for item in settings.auth_allowed_origins.split(",") if item.strip()}
    if (required and not origin) or (origin is not None and origin not in allowed):
        raise AccessError("origin_not_allowed", 403)


def _browser_post(request: Request) -> None:
    check_origin(request, required=True)
    origin = urlsplit(request.headers.get("origin", ""))
    if origin.scheme != "https" and not (
        settings.app_env == "development"
        and origin.scheme == "http"
        and origin.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise AccessError("secure_transport_required", 403)
    if request.headers.get("X-Huipi-Client") != "1":
        raise AccessError("csrf_failed", 403)


def _secure_cookie(request: Request) -> bool:
    # Only direct loopback development can use HTTP cookies; all other hosts need HTTPS.
    return not (
        settings.app_env == "development"
        and request.url.scheme == "http"
        and request.url.hostname in {"127.0.0.1", "localhost", "::1"}
        and request.client is not None
        and request.client.host in {"127.0.0.1", "::1", "localhost", "testclient"}
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


@router.post("/login", response_model=SessionRead)
async def local_login(body: LoginBody, request: Request, response: Response, db: Db):
    _browser_post(request)
    peer = request.client.host if request.client else "unknown"
    try:
        token, csrf, user_id, display_name, must_change, recovery_email = await local_auth.login(
            db, body.account, body.password, peer, hours=settings.auth_session_hours
        )
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError("authentication_unavailable", 503) from None
    common = {
        "secure": _secure_cookie(request),
        "samesite": "strict",
        "path": "/",
        "max_age": settings.auth_session_hours * 3600,
    }
    response.set_cookie(local_auth.SESSION_COOKIE, token, httponly=True, **common)
    response.set_cookie(local_auth.CSRF_COOKIE, csrf, httponly=False, **common)
    _no_store(response)
    return SessionRead(
        user_id=str(user_id),
        display_name=display_name,
        must_change_password=must_change,
        recovery_email=recovery_email,
    )


async def _authenticated(request: Request, *, unsafe: bool = False):
    token = request.cookies.get(local_auth.SESSION_COOKIE)
    if not token:
        raise AccessError("authentication_required", 401)
    if unsafe:
        _browser_post(request)
    verifier = local_auth.LocalSessionVerifier(settings)
    identity = await verifier.verify(
        token, csrf=request.headers.get("X-Huipi-CSRF"), require_csrf=unsafe
    )
    return token, identity


async def _require_current_session(db: AsyncSession, user_id, token: str) -> None:
    session_id = await db.scalar(
        select(LocalSession.token_hash)
        .where(
            LocalSession.token_hash == local_auth.token_hash(token),
            LocalSession.user_id == user_id,
            LocalSession.revoked_at.is_(None),
            LocalSession.expires_at > func.now(),
        )
        .with_for_update(read=True)
    )
    if not session_id:
        raise AccessError("invalid_session", 401)


@router.get("/session", response_model=SessionRead)
async def local_session(request: Request, response: Response, db: Db):
    _, identity = await _authenticated(request)
    row = (
        await db.execute(
            select(
                User.id,
                LocalCredential.display_name,
                LocalCredential.must_change_password,
                LocalCredential.recovery_email,
            )
            .join(LocalCredential, LocalCredential.user_id == User.id)
            .where(
                User.issuer == identity.issuer,
                User.subject == identity.subject,
                User.is_active.is_(True),
            )
        )
    ).first()
    if not row:
        raise AccessError("invalid_session", 401)
    _no_store(response)
    return SessionRead(
        user_id=str(row[0]), display_name=row[1], must_change_password=row[2], recovery_email=row[3]
    )


@router.post("/logout", status_code=204)
async def local_logout(request: Request, response: Response, db: Db):
    token, identity = await _authenticated(request, unsafe=True)
    async with db.begin():
        user = await db.scalar(
            select(User).where(User.issuer == identity.issuer, User.subject == identity.subject)
        )
        await db.execute(
            update(LocalSession)
            .where(LocalSession.token_hash == local_auth.token_hash(token))
            .values(revoked_at=func.now())
        )
        if user:
            db.add(LocalAuthAudit(actor_user_id=user.id, target_user_id=user.id, action="logout"))
    response.delete_cookie(
        local_auth.SESSION_COOKIE,
        path="/",
        secure=_secure_cookie(request),
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        local_auth.CSRF_COOKIE, path="/", secure=_secure_cookie(request), samesite="strict"
    )
    _no_store(response)


async def _safe_reset_mail(email: str, token: str) -> None:
    try:
        await mail.send(email, token, "reset_password")
    except Exception:
        # Do not log addresses, URLs, credentials, tokens or SMTP response details.
        logger.warning("password_reset_delivery_failed")


@router.post("/recovery-email/request", response_model=MessageRead)
async def request_recovery_email(body: EmailBody, request: Request, response: Response, db: Db):
    token_session, identity = await _authenticated(request, unsafe=True)
    if identity.must_change_password:
        raise AccessError("password_change_required", 403)
    try:
        email = local_auth.normalize_login(body.email)
        if "@" not in email:
            raise ValueError
    except ValueError:
        raise AccessError("invalid_email", 422) from None
    if not mail.configured():
        raise AccessError("mail_not_configured", 503)
    token = secrets.token_urlsafe(32)
    async with db.begin():
        user = await db.scalar(
            select(User)
            .where(
                User.issuer == identity.issuer,
                User.subject == identity.subject,
                User.is_active.is_(True),
            )
            .with_for_update()
        )
        if not user:
            raise AccessError("invalid_session", 401)
        await _require_current_session(db, user.id, token_session)
        allowed = await local_auth._throttle(db, "verify-email-user", str(user.id), 3)
        if allowed:
            db.add(
                LocalPasswordReset(
                    user_id=user.id,
                    token_hash=local_auth.token_hash(token),
                    purpose="verify_email",
                    target_email=email,
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                )
            )
    if not allowed:
        raise AccessError("mail_rate_limited", 429)
    try:
        await mail.send(email, token, "verify_email")
    except Exception:
        raise AccessError("mail_unavailable", 503) from None
    _no_store(response)
    return MessageRead(message="验证邮件已发送，请在30分钟内打开链接。")


@router.post("/recovery-email/confirm", status_code=204)
async def confirm_recovery_email(body: TokenBody, request: Request, response: Response, db: Db):
    token_session, identity = await _authenticated(request, unsafe=True)
    if identity.must_change_password or not local_auth._valid_token(body.token):
        raise AccessError("invalid_verification", 400)
    try:
        async with db.begin():
            user = await db.scalar(
                select(User)
                .where(
                    User.issuer == identity.issuer,
                    User.subject == identity.subject,
                    User.is_active.is_(True),
                )
                .with_for_update()
            )
            if not user:
                raise AccessError("invalid_session", 401)
            await _require_current_session(db, user.id, token_session)
            credential = await db.scalar(
                select(LocalCredential).where(LocalCredential.user_id == user.id).with_for_update()
            )
            item = await db.scalar(
                select(LocalPasswordReset)
                .where(
                    LocalPasswordReset.user_id == user.id,
                    LocalPasswordReset.token_hash == local_auth.token_hash(body.token),
                    LocalPasswordReset.purpose == "verify_email",
                    LocalPasswordReset.used_at.is_(None),
                    LocalPasswordReset.expires_at > func.now(),
                )
                .with_for_update()
            )
            if not credential or not item:
                raise AccessError("invalid_verification", 400)
            credential.recovery_email = item.target_email
            credential.recovery_verified_at = func.now()
            await db.execute(
                update(LocalPasswordReset)
                .where(LocalPasswordReset.user_id == user.id, LocalPasswordReset.used_at.is_(None))
                .values(used_at=func.now())
            )
            db.add(
                LocalAuthAudit(
                    actor_user_id=user.id, target_user_id=user.id, action="recovery_email_verified"
                )
            )
            await db.flush()
    except IntegrityError:
        raise AccessError("recovery_email_unavailable", 409) from None
    _no_store(response)


@router.post("/reset/request", response_model=MessageRead)
async def request_password_reset(
    body: ResetRequestBody,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    db: Db,
):
    _browser_post(request)
    try:
        account = local_auth.normalize_login(body.account)
    except ValueError:
        account = "invalid"
    peer = request.client.host if request.client else "unknown"
    token, email = secrets.token_urlsafe(32), None
    async with db.begin():
        allowed_ip = await local_auth._throttle(db, "reset-ip", peer, 20)
        allowed_account = (
            await local_auth._throttle(db, "reset-account", account, 4) if allowed_ip else False
        )
        if allowed_ip and allowed_account and mail.configured():
            row = (
                await db.execute(
                    select(User.id, LocalCredential.recovery_email)
                    .join(LocalCredential, LocalCredential.user_id == User.id)
                    .where(
                        LocalCredential.login_name == account,
                        LocalCredential.recovery_verified_at.is_not(None),
                        User.is_active.is_(True),
                    )
                )
            ).first()
            if row:
                email = row[1]
                db.add(
                    LocalPasswordReset(
                        user_id=row[0],
                        token_hash=local_auth.token_hash(token),
                        purpose="reset_password",
                        target_email=email,
                        expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    )
                )
    if email:
        background.add_task(_safe_reset_mail, email, token)
    _no_store(response)
    return MessageRead(
        message="如果账号已绑定找回邮箱并启用邮件服务，你将收到重置邮件；否则请联系管理员。"
    )


@router.post("/reset/confirm", status_code=204)
async def confirm_password_reset(
    body: ResetPasswordBody, request: Request, response: Response, db: Db
):
    _browser_post(request)
    if not local_auth._valid_token(body.token):
        raise AccessError("invalid_reset", 400)
    peer = request.client.host if request.client else "unknown"
    async with db.begin():
        allowed = await local_auth._throttle(db, "reset-confirm-ip", peer, 20)
        uid = (
            await db.scalar(
                select(LocalPasswordReset.user_id).where(
                    LocalPasswordReset.token_hash == local_auth.token_hash(body.token),
                    LocalPasswordReset.purpose == "reset_password",
                    LocalPasswordReset.used_at.is_(None),
                    LocalPasswordReset.expires_at > func.now(),
                )
            )
            if allowed
            else None
        )
    if uid is None:
        raise AccessError("invalid_reset", 400)
    try:
        password_hash = await local_auth.hash_password(body.password)
    except ValueError:
        raise AccessError("invalid_password", 422) from None
    async with db.begin():
        user = await db.scalar(
            select(User).where(User.id == uid, User.is_active.is_(True)).with_for_update()
        )
        credential = await db.scalar(
            select(LocalCredential).where(LocalCredential.user_id == uid).with_for_update()
        )
        item = await db.scalar(
            select(LocalPasswordReset)
            .where(
                LocalPasswordReset.token_hash == local_auth.token_hash(body.token),
                LocalPasswordReset.user_id == uid,
                LocalPasswordReset.purpose == "reset_password",
                LocalPasswordReset.used_at.is_(None),
                LocalPasswordReset.expires_at > func.now(),
            )
            .with_for_update()
        )
        if not user or not credential or not item or credential.recovery_email != item.target_email:
            raise AccessError("invalid_reset", 400)
        credential.password_hash, credential.must_change_password = password_hash, False
        credential.updated_at = func.now()
        await db.execute(
            update(LocalSession)
            .where(LocalSession.user_id == uid, LocalSession.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        await db.execute(
            update(LocalPasswordReset)
            .where(LocalPasswordReset.user_id == uid, LocalPasswordReset.used_at.is_(None))
            .values(used_at=func.now())
        )
        db.add(LocalAuthAudit(target_user_id=uid, action="password_reset_by_email"))
    _no_store(response)


@router.post("/change-password", status_code=204)
async def local_change_password(
    body: PasswordChangeBody, request: Request, response: Response, db: Db
):
    token, identity = await _authenticated(request, unsafe=True)
    async with db.begin():
        row = (
            await db.execute(
                select(User.id, LocalCredential.password_hash)
                .join(LocalCredential, LocalCredential.user_id == User.id)
                .where(
                    User.issuer == identity.issuer,
                    User.subject == identity.subject,
                    User.is_active.is_(True),
                )
            )
        ).first()
    if not row or not await local_auth.verify_password(row[1], body.current_password):
        raise AccessError("invalid_credentials", 401)
    try:
        new_hash = await local_auth.hash_password(body.new_password)
    except ValueError:
        raise AccessError("invalid_password", 422) from None
    async with db.begin():
        user = await db.scalar(
            select(User).where(User.id == row[0], User.is_active.is_(True)).with_for_update()
        )
        credential = await db.scalar(
            select(LocalCredential)
            .where(LocalCredential.user_id == row[0])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current = await db.scalar(
            select(LocalSession)
            .where(
                LocalSession.token_hash == local_auth.token_hash(token),
                LocalSession.revoked_at.is_(None),
                LocalSession.expires_at > func.now(),
            )
            .with_for_update()
        )
        if not user or not credential or credential.password_hash != row[1] or not current:
            raise AccessError("invalid_session", 401)
        credential.password_hash, credential.must_change_password = new_hash, False
        credential.updated_at = func.now()
        await db.execute(
            update(LocalSession)
            .where(LocalSession.user_id == user.id, LocalSession.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        await db.execute(
            update(LocalPasswordReset)
            .where(LocalPasswordReset.user_id == user.id, LocalPasswordReset.used_at.is_(None))
            .values(used_at=func.now())
        )
        db.add(
            LocalAuthAudit(actor_user_id=user.id, target_user_id=user.id, action="password_changed")
        )
    response.delete_cookie(
        local_auth.SESSION_COOKIE,
        path="/",
        secure=_secure_cookie(request),
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        local_auth.CSRF_COOKIE, path="/", secure=_secure_cookie(request), samesite="strict"
    )
    _no_store(response)
