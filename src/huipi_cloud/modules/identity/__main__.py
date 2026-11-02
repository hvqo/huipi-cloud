"""Local database operator provisioning. This CLI is not a teacher signup endpoint."""

import argparse
import asyncio
import getpass
import sys
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.infrastructure.database.base import utc_now
from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.modules.identity import local_auth
from huipi_cloud.modules.identity.models import (
    AssignmentMembership,
    LocalAuthAudit,
    LocalCredential,
    LocalPasswordReset,
    LocalSession,
    User,
)
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.identity.service import require_legacy_assignment


async def run(args):
    if session_factory is None:
        raise SystemExit("DATABASE_URL is required")
    bootstrap_user_id = None
    try:
        if args.command == "session-token":
            async with session_factory() as session:
                token, _, _, _, must_change, _ = await local_auth.login(
                    session, args.account, args.password, "local-cli", hours=1
                )
                if must_change:
                    async with session.begin():
                        await session.execute(
                            update(LocalSession)
                            .where(LocalSession.token_hash == local_auth.token_hash(token))
                            .values(revoked_at=func.now())
                        )
                    raise SystemExit("请先在网页修改初始密码")
                print(token)
                return
        async with session_factory() as session, session.begin():
            if args.command == "list-admins":
                rows = (
                    await session.execute(
                        select(User.id, User.is_active, LocalCredential.user_id)
                        .outerjoin(LocalCredential, LocalCredential.user_id == User.id)
                        .where(User.is_admin.is_(True))
                        .order_by(User.created_at, User.id)
                    )
                ).all()
                for user_id, active, credential_id in rows:
                    print(
                        f"user_id={user_id} active={active} local_login={credential_id is not None}"
                    )
            elif args.command == "bootstrap-local-admin":
                if not args.confirm_local_admin_bootstrap:
                    raise SystemExit("必须明确确认本地管理员初始化")
                await session.execute(select(func.pg_advisory_xact_lock(74685109321)))
                existing = await session.scalar(
                    select(User.id)
                    .join(LocalCredential, LocalCredential.user_id == User.id)
                    .where(User.is_admin.is_(True))
                )
                if existing and not args.existing_user_id:
                    raise SystemExit("本地管理员已初始化；不能重复创建")
                if args.existing_user_id:
                    user = await session.get(User, args.existing_user_id, with_for_update=True)
                    if not user or not user.is_admin or not user.is_active:
                        raise SystemExit("指定用户不是启用的现有管理员")
                else:
                    if await session.scalar(
                        select(User.id).where(User.is_admin.is_(True)).limit(1)
                    ):
                        raise SystemExit("已有管理员；必须明确指定要绑定的管理员用户")
                    user = User(
                        issuer="huipi:local", subject=str(uuid4()), is_admin=True, is_active=True
                    )
                    session.add(user)
                    await session.flush()
                if await session.get(LocalCredential, user.id):
                    raise SystemExit("该用户已有本地登录凭据")
                try:
                    account = local_auth.normalize_login(args.account)
                    password_hash = await local_auth.hash_password(args.password)
                except ValueError as error:
                    raise SystemExit(str(error)) from None
                session.add(
                    LocalCredential(
                        user_id=user.id,
                        login_name=account,
                        display_name=args.display_name,
                        password_hash=password_hash,
                        must_change_password=False,
                    )
                )
                session.add(
                    LocalAuthAudit(
                        target_user_id=user.id,
                        action="local_admin_bootstrap",
                    )
                )
                await session.flush()
                bootstrap_user_id = user.id
            elif args.command == "reset-local-admin-password":
                if not args.confirm_local_admin_password_reset:
                    raise SystemExit("必须明确确认管理员密码重置")
                user = await session.get(User, args.user_id, with_for_update=True)
                credential = await session.get(LocalCredential, args.user_id, with_for_update=True)
                if not user or not user.is_admin or not user.is_active or not credential:
                    raise SystemExit("指定用户不是已绑定本地账号的启用管理员")
                try:
                    credential.password_hash = await local_auth.hash_password(args.password)
                except ValueError as error:
                    raise SystemExit(str(error)) from None
                credential.must_change_password = True
                credential.updated_at = func.now()
                await session.execute(
                    update(LocalSession)
                    .where(LocalSession.user_id == user.id, LocalSession.revoked_at.is_(None))
                    .values(revoked_at=func.now())
                )
                await session.execute(
                    update(LocalPasswordReset)
                    .where(
                        LocalPasswordReset.user_id == user.id,
                        LocalPasswordReset.used_at.is_(None),
                    )
                    .values(used_at=func.now())
                )
                session.add(
                    LocalAuthAudit(target_user_id=user.id, action="local_admin_reset_by_operator")
                )
            elif args.command == "disable":
                await session.execute(
                    update(User)
                    .where(User.id == args.user_id)
                    .values(
                        is_active=False,
                    )
                )
                await session.execute(
                    update(LocalSession)
                    .where(LocalSession.user_id == args.user_id, LocalSession.revoked_at.is_(None))
                    .values(revoked_at=func.now())
                )
                await session.execute(
                    update(LocalPasswordReset)
                    .where(
                        LocalPasswordReset.user_id == args.user_id,
                        LocalPasswordReset.used_at.is_(None),
                    )
                    .values(used_at=func.now())
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
        if bootstrap_user_id:
            print(f"user_id={bootstrap_user_id}")
    finally:
        await dispose_database_engine()


def main():
    parser = argparse.ArgumentParser(description="仅限持有数据库凭据的本地操作员管理授权")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap-local-admin")
    bootstrap.add_argument("--account", required=True, help="管理员邮箱或手机号")
    bootstrap.add_argument("--display-name", required=True)
    bootstrap.add_argument("--existing-user-id", type=UUID)
    bootstrap.add_argument("--confirm-local-admin-bootstrap", action="store_true")
    commands.add_parser("list-admins", help="仅显示现有管理员内部编号与本地登录状态")
    reset_admin = commands.add_parser(
        "reset-local-admin-password", help="持有数据库权限的操作员重置现有管理员密码"
    )
    reset_admin.add_argument("--user-id", required=True, type=UUID)
    reset_admin.add_argument("--confirm-local-admin-password-reset", action="store_true")
    token = commands.add_parser("session-token", help="仅通过管道输出短时会话，密码从终端输入")
    token.add_argument("--account", required=True)
    disable = commands.add_parser("disable")
    disable.add_argument("--user-id", required=True, type=UUID)
    for name in ("grant", "revoke"):
        command = commands.add_parser(name)
        command.add_argument("--user-id", required=True, type=UUID)
        command.add_argument("--assignment-id", required=True, type=UUID)
        if name == "grant":
            command.add_argument("--write", action="store_true")
    try:
        args = parser.parse_args()
        if args.command in {"bootstrap-local-admin", "reset-local-admin-password", "session-token"}:
            if args.command == "session-token" and sys.stdout.isatty():
                raise SystemExit("请将会话输出通过管道传给受控命令，不要打印到终端")
            password = getpass.getpass(
                "管理员密码：" if args.command == "session-token" else "设置管理员密码："
            )
            if args.command != "session-token" and password != getpass.getpass("再次输入密码："):
                raise SystemExit("两次密码不一致")
            args.password = password
        asyncio.run(run(args))
    except AccessError as error:
        raise SystemExit(error.code) from None
    except (SQLAlchemyError, OSError):
        raise SystemExit("identity_operation_failed: 请检查数据库与输入；未输出敏感参数") from None


if __name__ == "__main__":
    main()
