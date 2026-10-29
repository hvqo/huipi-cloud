"""Explicit deployment bootstrap; not an HTTP or self-service grant."""

import argparse
import asyncio
import json
import sys
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.modules.identity.protocol import AccessError

from .final_service import enable_operator


def main():
    parser = argparse.ArgumentParser(description="受控启用成绩管理授权的部署操作员")
    parser.add_argument("--actor-user-id", required=True, type=UUID)
    parser.add_argument("--target-user-id", required=True, type=UUID)
    parser.add_argument("--request-id", required=True, type=UUID)
    parser.add_argument("--approval-ref", required=True)
    parser.add_argument("--confirm-deployment-operation", required=True, action="store_true")
    args = parser.parse_args()

    async def run():
        try:
            if session_factory is None:
                raise AccessError("database_unavailable", 503)
            async with session_factory() as db:
                value = await enable_operator(
                    db, args.actor_user_id, args.target_user_id, args.request_id, args.approval_ref
                )
            print(json.dumps(value))
            return 0
        except (AccessError, SQLAlchemyError, OSError):
            print('{"status":"failed","code":"final_deployment_failed"}', file=sys.stderr)
            return 1
        finally:
            await dispose_database_engine()

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
