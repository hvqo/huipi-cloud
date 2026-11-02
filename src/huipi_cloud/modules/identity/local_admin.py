"""Administrator-managed teacher credentials; never grants administrator status."""

import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from huipi_cloud.modules.assignments.router import DbSession
from huipi_cloud.modules.identity import admin_service, local_auth
from huipi_cloud.modules.identity.admin_router import IdentityRoute
from huipi_cloud.modules.identity.dependencies import AuthorizedPrincipal
from huipi_cloud.modules.identity.models import (
    LocalAuthAudit,
    LocalCredential,
    LocalPasswordReset,
    LocalSession,
    User,
)
from huipi_cloud.modules.identity.protocol import AccessError

router = APIRouter(
    prefix="/admin/local-accounts", tags=["local-account-administration"], route_class=IdentityRoute
)


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    account: str = Field(min_length=3, max_length=254)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=6, max_length=128, repr=False)


class PasswordResetByAdmin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    password: str = Field(min_length=6, max_length=128, repr=False)


class LocalAccountItem(BaseModel):
    user_id: UUID
    account: str
    display_name: str | None
    is_active: bool
    must_change_password: bool


class LocalAccountPage(BaseModel):
    records: list[LocalAccountItem]
    offset: int
    limit: int
    has_next: bool


class AccountWriteResult(BaseModel):
    user_id: UUID
    request_id: UUID
    action: str


class LocalAuditItem(BaseModel):
    event_id: UUID
    actor_user_id: UUID | None
    target_user_id: UUID
    action: str
    request_id: UUID | None
    created_at: datetime


class LocalAuditPage(BaseModel):
    records: list[LocalAuditItem]
    offset: int
    limit: int
    has_next: bool


def _digest(action: str, **payload) -> str:
    # Never include a password or an unsalted fast password digest in an audit record.
    content = json.dumps(
        {"action": action, **payload}, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(content.encode()).hexdigest()


async def _replay(db, actor_id, request_id, digest, password):
    lock_key = int.from_bytes(
        hashlib.sha256(f"local-auth:{actor_id}:{request_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    await db.execute(select(func.pg_advisory_xact_lock(lock_key)))
    audit = await db.scalar(
        select(LocalAuthAudit).where(
            LocalAuthAudit.actor_user_id == actor_id, LocalAuthAudit.request_id == request_id
        )
    )
    if not audit:
        return None
    credential = await db.get(LocalCredential, audit.target_user_id)
    if (
        audit.payload_sha256 != digest
        or not credential
        or not await local_auth.verify_password(credential.password_hash, password)
    ):
        raise AccessError("account_request_conflict", 409)
    return AccountWriteResult(
        user_id=audit.target_user_id, request_id=request_id, action=audit.action
    )


async def _append_audit(db, *, actor_id, target_id, action, request_id, digest):
    db.add(
        LocalAuthAudit(
            actor_user_id=actor_id,
            target_user_id=target_id,
            action=action,
            request_id=request_id,
            payload_sha256=digest,
        )
    )
    await db.flush()


@router.get("/audit", response_model=LocalAuditPage)
async def list_local_account_audit(
    db: DbSession,
    principal: AuthorizedPrincipal,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
):
    async with admin_service.administrator_transaction(db, principal):
        rows = (
            await db.scalars(
                select(LocalAuthAudit)
                .order_by(LocalAuthAudit.created_at.desc(), LocalAuthAudit.id)
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
        return LocalAuditPage(
            records=[
                LocalAuditItem(
                    event_id=r.id,
                    actor_user_id=r.actor_user_id,
                    target_user_id=r.target_user_id,
                    action=r.action,
                    request_id=r.request_id,
                    created_at=r.created_at,
                )
                for r in rows[:limit]
            ],
            offset=offset,
            limit=limit,
            has_next=len(rows) > limit,
        )


@router.get("", response_model=LocalAccountPage)
async def list_local_accounts(
    db: DbSession,
    principal: AuthorizedPrincipal,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
):
    async with admin_service.administrator_transaction(db, principal):
        rows = (
            await db.execute(
                select(User, LocalCredential)
                .join(LocalCredential, LocalCredential.user_id == User.id)
                .where(User.is_admin.is_(False))
                .order_by(User.created_at, User.id)
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
        return LocalAccountPage(
            records=[
                LocalAccountItem(
                    user_id=user.id,
                    account=credential.login_name,
                    display_name=credential.display_name,
                    is_active=user.is_active,
                    must_change_password=credential.must_change_password,
                )
                for user, credential in rows[:limit]
            ],
            offset=offset,
            limit=limit,
            has_next=len(rows) > limit,
        )


@router.post("", response_model=AccountWriteResult, status_code=201)
async def create_local_account(body: AccountCreate, db: DbSession, principal: AuthorizedPrincipal):
    try:
        account = local_auth.normalize_login(body.account)
        password_hash = await local_auth.hash_password(body.password)
    except ValueError:
        raise AccessError("invalid_account_or_password", 422) from None
    name = body.display_name.strip()
    if not name:
        raise AccessError("invalid_input", 422)
    action = "local_account_created"
    digest = _digest(action, account=account, display_name=name)
    try:
        async with admin_service.administrator_transaction(db, principal) as current:
            old = await _replay(db, current.user_id, body.request_id, digest, body.password)
            if old:
                return old
            user = User(issuer="huipi:local", subject=str(uuid4()), is_admin=False, is_active=True)
            db.add(user)
            await db.flush()
            db.add(
                LocalCredential(
                    user_id=user.id,
                    login_name=account,
                    display_name=name,
                    password_hash=password_hash,
                    must_change_password=True,
                )
            )
            await _append_audit(
                db,
                actor_id=current.user_id,
                target_id=user.id,
                action=action,
                request_id=body.request_id,
                digest=digest,
            )
            return AccountWriteResult(user_id=user.id, request_id=body.request_id, action=action)
    except IntegrityError:
        raise AccessError("account_already_exists", 409) from None


@router.post("/{user_id}/bind", response_model=AccountWriteResult)
async def bind_local_account(
    user_id: UUID, body: AccountCreate, db: DbSession, principal: AuthorizedPrincipal
):
    """Explicitly attach credentials to one existing teacher without changing their ID/grants."""
    try:
        account = local_auth.normalize_login(body.account)
        password_hash = await local_auth.hash_password(body.password)
    except ValueError:
        raise AccessError("invalid_account_or_password", 422) from None
    name = body.display_name.strip()
    if not name:
        raise AccessError("invalid_input", 422)
    action = "local_account_bound"
    digest = _digest(action, target_user_id=str(user_id), account=account, display_name=name)
    try:
        async with admin_service.administrator_transaction(db, principal) as current:
            old = await _replay(db, current.user_id, body.request_id, digest, body.password)
            if old:
                return old
            user = await admin_service.teacher(db, user_id, lock=True)
            if not user.is_active or await db.get(LocalCredential, user_id):
                raise AccessError("account_not_bindable", 409)
            db.add(
                LocalCredential(
                    user_id=user.id,
                    login_name=account,
                    display_name=name,
                    password_hash=password_hash,
                    must_change_password=True,
                )
            )
            await _append_audit(
                db,
                actor_id=current.user_id,
                target_id=user.id,
                action=action,
                request_id=body.request_id,
                digest=digest,
            )
            return AccountWriteResult(user_id=user.id, request_id=body.request_id, action=action)
    except IntegrityError:
        raise AccessError("account_already_exists", 409) from None


@router.post("/{user_id}/reset-password", response_model=AccountWriteResult)
async def reset_local_account(
    user_id: UUID, body: PasswordResetByAdmin, db: DbSession, principal: AuthorizedPrincipal
):
    try:
        password_hash = await local_auth.hash_password(body.password)
    except ValueError:
        raise AccessError("invalid_password", 422) from None
    action = "local_password_reset_by_admin"
    digest = _digest(action, target_user_id=str(user_id))
    async with admin_service.administrator_transaction(db, principal) as current:
        old = await _replay(db, current.user_id, body.request_id, digest, body.password)
        if old:
            return old
        target = await admin_service.teacher(db, user_id, lock=True)
        credential = await db.scalar(
            select(LocalCredential).where(LocalCredential.user_id == target.id).with_for_update()
        )
        if not credential:
            raise AccessError("account_not_found", 404)
        credential.password_hash, credential.must_change_password = password_hash, True
        credential.updated_at = func.now()
        await db.execute(
            update(LocalSession)
            .where(LocalSession.user_id == target.id, LocalSession.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        await db.execute(
            update(LocalPasswordReset)
            .where(LocalPasswordReset.user_id == target.id, LocalPasswordReset.used_at.is_(None))
            .values(used_at=func.now())
        )
        await _append_audit(
            db,
            actor_id=current.user_id,
            target_id=target.id,
            action=action,
            request_id=body.request_id,
            digest=digest,
        )
        return AccountWriteResult(user_id=target.id, request_id=body.request_id, action=action)
