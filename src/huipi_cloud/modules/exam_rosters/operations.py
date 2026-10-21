"""Roster idempotency and append-only audit in the business transaction."""

import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import func, insert, select
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.modules.exams.service import digest
from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.protocol import AccessError

from .models import RosterOperation

OPS = RosterOperation.__table__


@dataclass
class Operation:
    replay: dict | None
    result: dict | None = None


@asynccontextmanager
async def operation(session, principal, request_id, action, payload):
    fingerprint = digest({"action": action, "payload": payload})
    committing = False
    try:
        await session.begin()
        await session.execute(select(func.set_config("statement_timeout", "8000", True)))
        await identity.refresh_principal(session, principal, lock=True)
        key = int.from_bytes(
            hashlib.sha256(f"roster:{principal.user_id}:{request_id}".encode()).digest()[:8],
            signed=True,
        )
        await session.execute(select(func.pg_advisory_xact_lock(key)))
        old = (
            (
                await session.execute(
                    select(OPS).where(
                        OPS.c.actor_user_id == principal.user_id,
                        OPS.c.request_id == request_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if old and old["payload_sha256"] != fingerprint:
            raise AccessError("roster_request_conflict", 409)
        op = Operation(dict(old["result"]) if old else None)
        yield op
        if op.replay is None:
            if op.result is None:
                raise RuntimeError("exam operation missing result")
            await session.execute(
                insert(OPS).values(
                    id=uuid4(),
                    actor_user_id=principal.user_id,
                    request_id=request_id,
                    action=action,
                    payload_sha256=fingerprint,
                    result=op.result,
                )
            )
        committing = True
        await session.commit()
    except BaseException as error:
        try:
            await session.rollback()
        except (SQLAlchemyError, OSError, TimeoutError):
            pass
        if isinstance(error, (SQLAlchemyError, OSError, TimeoutError)):
            code = getattr(getattr(error, "orig", None), "sqlstate", None)
            if not committing and code in {"23505", "23514", "23503", "40001", "40P01"}:
                raise AccessError("roster_write_conflict", 409) from None
            raise AccessError(
                "roster_commit_outcome_unknown" if committing else "roster_operation_unavailable",
                503,
            ) from None
        raise
