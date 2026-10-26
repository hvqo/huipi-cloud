"""Controlled class provisioning with verified JWT on stdin, never an admin-role claim."""

import argparse
import asyncio
import json
import sys
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.auth.jwt import TokenVerifier
from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.identity.service import resolve_principal

from . import access


async def run(args):
    if session_factory is None or not args.token_stdin:
        raise AccessError("authentication_required", 401)
    identity = await TokenVerifier(settings).verify(sys.stdin.readline(16386).strip())
    async with session_factory() as db:
        p = await resolve_principal(db, identity)
        await db.rollback()
        if args.command == "grant-class":
            return await access.grant_class(
                db,
                p,
                args.request_id,
                grant_id=args.grant_id,
                session_id=args.session_id,
                class_id=args.class_id,
                export_allowed=args.export_allowed,
            )
        return await access.revoke_class(db, p, args.request_id, args.class_access_id)


def main():
    parser = argparse.ArgumentParser(description="内部成绩预览班级授权；不会授予全局管理员")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["grant-class", "revoke-class"]:
        command = commands.add_parser(name)
        command.add_argument("--token-stdin", action="store_true")
        command.add_argument("--request-id", type=UUID, required=True)
        if name == "grant-class":
            for argument in ["grant-id", "session-id", "class-id"]:
                command.add_argument("--" + argument, type=UUID, required=True)
            command.add_argument("--export-allowed", action="store_true")
        else:
            command.add_argument("--class-access-id", type=UUID, required=True)

    async def execute():
        try:
            print(json.dumps(await run(parser.parse_args())))
            return 0
        except AccessError as error:
            print(json.dumps(dict(status="failed", code=error.code)), file=sys.stderr)
            return 1
        except (SQLAlchemyError, OSError, ValueError):
            print('{"status":"failed","code":"result_access_unavailable"}', file=sys.stderr)
            return 1
        finally:
            await dispose_database_engine()

    raise SystemExit(asyncio.run(execute()))


if __name__ == "__main__":
    main()
