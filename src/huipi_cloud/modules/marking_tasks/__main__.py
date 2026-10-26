"""Deployment-only role bootstrap; never called by the teacher HTTP API."""

import argparse
import asyncio
from uuid import UUID

from huipi_cloud.infrastructure.database.session import session_factory

from .service import enable_operator


async def main():
    parser = argparse.ArgumentParser(description="受控部署：批准单评任务岗位委派能力")
    parser.add_argument("--actor-user-id", type=UUID, required=True)
    parser.add_argument("--target-user-id", type=UUID, required=True)
    parser.add_argument("--request-id", type=UUID, required=True)
    parser.add_argument("--approval-ref", required=True)
    args = parser.parse_args()
    if session_factory is None:
        raise SystemExit("数据库未配置")
    async with session_factory() as db:
        result = await enable_operator(
            db, args.actor_user_id, args.target_user_id, args.request_id, args.approval_ref
        )
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
