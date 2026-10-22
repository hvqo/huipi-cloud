"""Deployment approval + database credentials; no public self-service enrollment."""

import argparse
import asyncio
import json
from uuid import UUID

from huipi_cloud.infrastructure.database.session import session_factory
from huipi_cloud.modules.identity.protocol import AccessError

from .service import enable_operator


async def run(args):
    if session_factory is None:
        raise AccessError("scan_unavailable", 503)
    async with session_factory() as db:
        return await enable_operator(
            db, args.actor_user_id, args.target_user_id, args.request_id, args.approval_ref
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["enable-operator"])
    parser.add_argument("--actor-user-id", type=UUID, required=True)
    parser.add_argument("--target-user-id", type=UUID, required=True)
    parser.add_argument("--request-id", type=UUID, required=True)
    parser.add_argument("--approval-ref", required=True)
    parser.add_argument("--confirm-deployment-operation", action="store_true", required=True)
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args()))))
    except AccessError as error:
        print(json.dumps({"error": error.code}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
