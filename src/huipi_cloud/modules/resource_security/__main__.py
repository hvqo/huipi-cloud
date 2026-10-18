"""Explicit operator CLI; no anonymous or JWT-role-based provisioning."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.auth.jwt import TokenVerifier
from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.modules.assignments.schemas import AssignmentCreate
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.identity.service import resolve_principal
from huipi_cloud.modules.resource_security import service
from huipi_cloud.modules.resource_security.models import SecurityAudit


async def run(args):
    if session_factory is None:
        raise AccessError("authorization_unavailable", 503)
    async with session_factory() as session:
        if args.command == "bootstrap":
            if not args.confirm_deployment_bootstrap:
                raise AccessError("security_bootstrap_confirmation_required")
            return await service.deployment_bootstrap(
                session,
                args.user_id,
                args.request_id,
                args.approval_ref,
            )
        if not args.token_stdin:
            raise AccessError("authentication_required", 401)
        token = sys.stdin.readline(16386).strip()
        identity = await TokenVerifier(settings).verify(token)
        principal = await resolve_principal(session, identity)
        await session.rollback()
        if args.command == "grant":
            return await service.grant_role(
                session,
                principal,
                args.request_id,
                user_id=args.user_id,
                role_version_id=args.role_version_id,
                scope_id=args.scope_id,
            )
        if args.command == "revoke":
            return await service.revoke_grant(
                session, principal, args.request_id, grant_id=args.grant_id
            )
        if args.command == "create-protected":
            with Path(args.payload).open("rb") as file:
                raw = file.read(8193)
            if len(raw) > 8192:
                raise AccessError("security_payload_too_large", 413)
            payload = AssignmentCreate.model_validate_json(raw)
            return await service.create_protected_assignment(
                session,
                principal,
                args.request_id,
                payload,
            )
        if args.command == "inspect-classification":
            return await service.inspect_classification(
                session,
                principal,
                args.assignment_id,
                args.scope_id,
            )
        async with session.begin():
            await service.require_permission(
                session, principal, "security.grants.manage", service.INSTALLATION_SCOPE
            )
            rows = (
                await session.scalars(
                    select(SecurityAudit)
                    .order_by(SecurityAudit.created_at.desc(), SecurityAudit.id)
                    .limit(50)
                )
            ).all()
            return {
                "events": [
                    dict(
                        event_id=str(r.id),
                        actor_user_id=str(r.actor_user_id),
                        request_id=str(r.request_id),
                        action=r.action,
                        result=r.result,
                        created_at=r.created_at.isoformat(),
                    )
                    for r in rows
                ],
                "limit": 50,
            }


def main():
    parser = argparse.ArgumentParser(description="受控部署/安全CLI；不授予任何考试作答读取能力")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (
        "bootstrap",
        "grant",
        "revoke",
        "create-protected",
        "inspect-classification",
        "audit",
    ):
        cmd = sub.add_parser(name)
        if name == "bootstrap":
            cmd.add_argument("--confirm-deployment-bootstrap", action="store_true")
            cmd.add_argument("--approval-ref", required=True)
        else:
            cmd.add_argument("--token-stdin", action="store_true")
        if name in {"bootstrap", "grant", "revoke", "create-protected"}:
            cmd.add_argument("--request-id", type=UUID, required=True)
        if name in {"bootstrap", "grant"}:
            cmd.add_argument("--user-id", type=UUID, required=True)
        if name == "grant":
            cmd.add_argument("--role-version-id", type=UUID, required=True)
        if name in {"grant", "inspect-classification"}:
            cmd.add_argument("--scope-id", type=UUID, required=True)
        if name == "revoke":
            cmd.add_argument("--grant-id", type=UUID, required=True)
        if name == "create-protected":
            cmd.add_argument("--payload", required=True)
        if name == "inspect-classification":
            cmd.add_argument("--assignment-id", type=UUID, required=True)

    async def execute():
        try:
            print(json.dumps(await run(parser.parse_args()), ensure_ascii=False))
            return 0
        except AccessError as error:
            print(json.dumps({"status": "failed", "code": error.code}), file=sys.stderr)
            return 1
        except (SQLAlchemyError, OSError, TimeoutError, ValidationError, ValueError):
            print('{"status":"failed","code":"security_operation_unavailable"}', file=sys.stderr)
            return 1
        finally:
            await dispose_database_engine()

    raise SystemExit(asyncio.run(execute()))


if __name__ == "__main__":
    main()
