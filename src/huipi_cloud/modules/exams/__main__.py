"""Controlled operator CLI. JWT comes from stdin, not arguments; no public HTTP."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.auth.jwt import TokenVerifier
from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.modules.exams import service
from huipi_cloud.modules.exams.schemas import (
    EventCreate,
    EventEdit,
    EventState,
    PaperBind,
    PaperUnbind,
    SessionCreate,
    SessionEdit,
    SessionState,
)
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.identity.service import resolve_principal

COMMANDS = {
    "create-event": (EventCreate, service.create_event),
    "edit-event": (EventEdit, service.edit_event),
    "event-state": (EventState, service.change_event_state),
    "create-session": (SessionCreate, service.create_session),
    "edit-session": (SessionEdit, service.edit_session),
    "session-state": (SessionState, service.change_session_state),
    "bind": (PaperBind, service.bind_paper),
    "unbind": (PaperUnbind, service.unbind_paper),
}


async def run(args):
    if session_factory is None:
        raise AccessError("exam_operation_unavailable", 503)
    async with session_factory() as session:
        if args.command == "enable-operator":
            if not args.confirm_deployment_operation:
                raise AccessError("exam_deployment_confirmation_required")
            return await service.enable_operator(
                session,
                args.actor_user_id,
                args.target_user_id,
                args.request_id,
                args.approval_ref,
            )
        if not args.token_stdin:
            raise AccessError("authentication_required", 401)
        identity = await TokenVerifier(settings).verify(sys.stdin.readline(16386).strip())
        principal = await resolve_principal(session, identity)
        await session.rollback()
        if args.command == "inspect-event":
            return await service.inspect_event(session, principal, args.id)
        if args.command == "inspect-session":
            return await service.inspect_session(session, principal, args.id)
        model, execute = COMMANDS[args.command]
        with Path(args.payload).open("rb") as file:
            raw = file.read(service.MAX_CONTENT_BYTES + 1)
        if len(raw) > service.MAX_CONTENT_BYTES:
            raise AccessError("exam_payload_too_large", 413)
        return await execute(session, principal, model.model_validate_json(raw))


def main():
    parser = argparse.ArgumentParser(description="受控考试计划CLI；不包含参试名单、扫描或学生作答")
    sub = parser.add_subparsers(dest="command", required=True)
    enable = sub.add_parser("enable-operator")
    for name in ("actor-user-id", "target-user-id", "request-id"):
        enable.add_argument("--" + name, type=UUID, required=True)
    enable.add_argument("--approval-ref", required=True)
    enable.add_argument("--confirm-deployment-operation", action="store_true")
    for name in ("inspect-event", "inspect-session", *COMMANDS):
        command = sub.add_parser(name)
        command.add_argument("--token-stdin", action="store_true")
        if name.startswith("inspect-"):
            command.add_argument("--id", type=UUID, required=True)
        else:
            command.add_argument("--payload", required=True)

    async def execute():
        try:
            print(json.dumps(await run(parser.parse_args()), ensure_ascii=False))
        except AccessError as error:
            print(json.dumps({"status": "failed", "code": error.code}), file=sys.stderr)
            return 1
        except (SQLAlchemyError, OSError, TimeoutError, ValidationError, ValueError):
            print('{"status":"failed","code":"exam_operation_unavailable"}', file=sys.stderr)
            return 1
        finally:
            await dispose_database_engine()
        return 0

    raise SystemExit(asyncio.run(execute()))


if __name__ == "__main__":
    main()
