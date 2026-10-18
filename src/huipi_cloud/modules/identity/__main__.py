"""Local database operator provisioning. This CLI is not a teacher signup endpoint."""

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.database.base import utc_now
from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.modules.identity.models import AssignmentMembership, User
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.identity.service import require_legacy_assignment


async def run(args):
    if session_factory is None:
        raise SystemExit("DATABASE_URL is required")
    try:
        async with session_factory() as session, session.begin():
            if args.command == "provision":
                if not settings.auth_issuer or not args.subject.strip() or len(args.subject) > 255:
                    raise SystemExit("需要有效 AUTH_ISSUER 和 subject")
                # Repeating provisioning cannot accidentally demote/reactivate an existing user.
                await session.execute(
                    insert(User)
                    .values(
                        issuer=settings.auth_issuer,
                        subject=args.subject,
                        is_admin=args.admin,
                        is_active=True,
                    )
                    .on_conflict_do_nothing(constraint="uq_users_issuer_subject")
                )
                user_id = await session.scalar(
                    select(User.id).where(
                        User.issuer == settings.auth_issuer,
                        User.subject == args.subject,
                    )
                )
                print(f"user_id={user_id}")
            elif args.command == "disable":
                await session.execute(
                    update(User)
                    .where(User.id == args.user_id)
                    .values(
                        is_active=False,
                    )
                )
            elif args.command == "grant":
                await require_legacy_assignment(session, args.assignment_id)
                await session.execute(
                    insert(AssignmentMembership)
                    .values(
                        assignment_id=args.assignment_id,
                        user_id=args.user_id,
                        can_write=args.write,
                    )
                    .on_conflict_do_update(
                        constraint="uq_assignment_memberships_user",
                        set_={
                            "can_write": args.write,
                            "revoked_at": None,
                        },
                    )
                )
            else:
                await require_legacy_assignment(session, args.assignment_id)
                await session.execute(
                    update(AssignmentMembership)
                    .where(
                        AssignmentMembership.assignment_id == args.assignment_id,
                        AssignmentMembership.user_id == args.user_id,
                    )
                    .values(revoked_at=utc_now())
                )
    finally:
        await dispose_database_engine()


def main():
    parser = argparse.ArgumentParser(description="仅限持有数据库凭据的本地操作员管理授权")
    commands = parser.add_subparsers(dest="command", required=True)
    provision = commands.add_parser("provision")
    provision.add_argument("--subject", required=True)
    provision.add_argument("--admin", action="store_true", help="仅新建用户时授予本地管理员")
    disable = commands.add_parser("disable")
    disable.add_argument("--user-id", required=True, type=UUID)
    for name in ("grant", "revoke"):
        command = commands.add_parser(name)
        command.add_argument("--user-id", required=True, type=UUID)
        command.add_argument("--assignment-id", required=True, type=UUID)
        if name == "grant":
            command.add_argument("--write", action="store_true")
    try:
        asyncio.run(run(parser.parse_args()))
    except AccessError as error:
        raise SystemExit(error.code) from None
    except (SQLAlchemyError, OSError):
        raise SystemExit("identity_operation_failed: 请检查数据库与输入；未输出敏感参数") from None


if __name__ == "__main__":
    main()
