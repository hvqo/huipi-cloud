"""Organization write audit, idempotency and commit-unknown recovery."""

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.organization.models import OrganizationOperation
from huipi_cloud.modules.resource_security import service as security

INSTALLATION_ACTIONS = {
    "organization.read",
    "organization.manage",
    "student.import.preview",
    "student.import.confirm",
}
COMMIT_OUTCOME_UNKNOWN = "organization_commit_outcome_unknown"
OPERATION_UNAVAILABLE = "organization_operation_unavailable"


@dataclass
class Operation:
    replay: dict | None
    result: dict | None = None


def payload_digest(action: str, payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            {"action": action, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


async def authorize(session, principal, action: str) -> None:
    """Only the fixed installation scope exists in B-1.1b; no class/exam UUID scopes."""
    if action not in INSTALLATION_ACTIONS:
        raise AccessError("organization_permission_disabled")
    try:
        root = await security.installation_scope(session)
        await security.require_permission(session, principal, action, root.id)
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError(OPERATION_UNAVAILABLE, 503) from None


@asynccontextmanager
async def operation(session, principal, request_id, action: str, payload: dict):
    digest = payload_digest(action, payload)
    commit_started = False
    try:
        await session.begin()
        await session.execute(select(func.set_config("statement_timeout", "5000", True)))
        await identity.refresh_principal(session, principal, lock=True)
        lock_key = int.from_bytes(
            hashlib.sha256(
                f"organization:{principal.user_id}:{request_id}".encode()
            ).digest()[:8],
            signed=True,
        )
        await session.execute(select(func.pg_advisory_xact_lock(lock_key)))
        previous = await session.scalar(
            select(OrganizationOperation).where(
                OrganizationOperation.actor_user_id == principal.user_id,
                OrganizationOperation.request_id == request_id,
            )
        )
        if previous is not None and previous.payload_sha256 != digest:
            raise AccessError("organization_request_conflict", 409)
        op = Operation(previous.result if previous else None)
        yield op
        if op.replay is None:
            if op.result is None:
                raise RuntimeError("organization operation missing result")
            session.add(
                OrganizationOperation(
                    actor_user_id=principal.user_id,
                    request_id=request_id,
                    action=action,
                    payload_sha256=digest,
                    result=op.result,
                )
            )
            await session.flush()
        commit_started = True
        await session.commit()
    except BaseException as error:
        try:
            await session.rollback()
        except (SQLAlchemyError, OSError, TimeoutError):
            pass
        if isinstance(error, (SQLAlchemyError, OSError, TimeoutError)):
            raise AccessError(
                COMMIT_OUTCOME_UNKNOWN if commit_started else OPERATION_UNAVAILABLE,
                503,
            ) from None
        raise
