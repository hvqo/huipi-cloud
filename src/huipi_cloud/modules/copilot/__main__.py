"""Run one locally scoped conversation; cloud transfer requires explicit configuration."""

import argparse
import asyncio
import json
import sys
from uuid import UUID

from pydantic import ValidationError

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.copilot.provider import DeepSeekCopilotProvider
from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.modules.copilot.protocol import CopilotError, CopilotRequest
from huipi_cloud.modules.copilot.runtime import CopilotRuntime
from huipi_cloud.modules.copilot.tools import BusinessTools


async def run(args):
    config = Settings()
    # Explicit, optional reuse of the locally configured key, never silent fallback.
    if args.use_grading_key:
        config = config.model_copy(update={"copilot_api_key": config.grading_api_key})
    try:
        request = CopilotRequest(assignment_id=args.assignment_id, question=args.question)
        provider = DeepSeekCopilotProvider(config)
        runtime = CopilotRuntime(
            provider, BusinessTools(session_factory, request.assignment_id), config
        )
        result = await runtime.run(request)
        print(result.model_dump_json(indent=2))
        return 0 if result.status == "completed" else 1
    except (CopilotError, ValidationError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": error.code if isinstance(error, CopilotError) else "invalid_cli_input",
                }
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        await dispose_database_engine()


def main():
    parser = argparse.ArgumentParser(description="受控本地教师Copilot；没有身份认证或成绩发布权限")
    parser.add_argument("--assignment-id", required=True, type=UUID)
    parser.add_argument("--question", required=True)
    parser.add_argument(
        "--use-grading-key",
        action="store_true",
        help="显式复用本地GRADING_API_KEY；不输出或复制密钥",
    )
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
