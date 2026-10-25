"""Operator-configured exam scoring; never selects a model or falls back implicitly."""

import asyncio
import logging
import signal

from huipi_cloud.core.config import settings
from huipi_cloud.core.logging import configure_logging
from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.modules.exam_grading.runtime import ScoringWorker


async def run():
    if session_factory is None:
        raise ValueError("DATABASE_URL is required")
    config = settings.model_copy(update={"grading_invalid_response_retries": 0})
    provider = build_text_grading_provider(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await ScoringWorker(session_factory, get_object_storage(), provider, config).run(stop)
    finally:
        await dispose_database_engine()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main():
    configure_logging(settings.log_level)
    try:
        asyncio.run(run())
    except Exception as error:
        logging.getLogger(__name__).error(
            "Exam scoring stopped error_type=%s", type(error).__name__
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
