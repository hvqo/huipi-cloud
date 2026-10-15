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
from huipi_cloud.infrastructure.embedding.protocol import EmbeddingError
from huipi_cloud.infrastructure.storage.dependencies import _configured_storage
from huipi_cloud.modules.copilot.protocol import CopilotError, CopilotRequest
from huipi_cloud.modules.copilot.runtime import CopilotRuntime
from huipi_cloud.modules.copilot.tools import BusinessTools
from huipi_cloud.modules.knowledge.dependencies import configured_retriever
from huipi_cloud.modules.knowledge.schemas import KnowledgeError


async def run(args):
    config = Settings()
    # Explicit, optional reuse of the locally configured key, never silent fallback.
    if args.use_grading_key:
        config = config.model_copy(update={"copilot_api_key": config.grading_api_key})
    retriever = None
    try:
        request = CopilotRequest(
            assignment_id=args.assignment_id,
            question=args.question,
            require_material_evidence=getattr(args, "require_material_evidence", False),
        )
        provider = DeepSeekCopilotProvider(config)
        try:
            storage = _configured_storage()
        except ValueError:
            storage = None  # The original four tools do not require object storage.
        retriever = configured_retriever(session_factory, storage, config)
        if hasattr(retriever, "prepare"):
            # Warm local model/index before any paid call or 5s Agent tool deadline.
            await retriever.prepare(request.assignment_id)
        runtime = CopilotRuntime(
            provider,
            BusinessTools(
                session_factory,
                request.assignment_id,
                storage=storage,
                retriever=retriever,
                max_result_bytes=config.copilot_max_tool_result_bytes,
            ),
            config,
        )
        result = await runtime.run(request)
        print(result.model_dump_json(indent=2))
        return 0 if result.status == "completed" else 1
    except (CopilotError, KnowledgeError, EmbeddingError, ValidationError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": error.code
                    if isinstance(error, (CopilotError, KnowledgeError, EmbeddingError))
                    else "invalid_cli_input",
                }
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if retriever is not None and hasattr(retriever, "aclose"):
            await retriever.aclose()
        await dispose_database_engine()


def main():
    parser = argparse.ArgumentParser(description="受控本地教师Copilot；没有身份认证或成绩发布权限")
    parser.add_argument("--assignment-id", required=True, type=UUID)
    parser.add_argument("--question", required=True)
    parser.add_argument(
        "--require-material-evidence",
        action="store_true",
        help="材料问答严格模式：即使模型跳过检索也不得无证据完成回答",
    )
    parser.add_argument(
        "--use-grading-key",
        action="store_true",
        help="显式复用本地GRADING_API_KEY；不输出或复制密钥",
    )
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
