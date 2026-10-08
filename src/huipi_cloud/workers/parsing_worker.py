"""Command-line entry point for the standalone parsing worker process."""

import asyncio
import logging
import signal

from huipi_cloud.core.config import settings
from huipi_cloud.core.logging import configure_logging
from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.modules.parsing.executor import close_executor, load_executor
from huipi_cloud.workers.parsing import ParsingWorker

logger = logging.getLogger(__name__)


class WorkerConfigurationError(RuntimeError):
    """Raised when a worker cannot safely start with its current configuration."""


async def run_worker() -> None:
    """Run the configured production parser, or fail closed before polling."""
    if not settings.parsing_executor:
        raise WorkerConfigurationError(
            "No production parser is configured; set PARSING_EXECUTOR=module.path:factory "
            "before starting a parsing worker"
        )
    if session_factory is None:
        raise WorkerConfigurationError("DATABASE_URL is required to start the parsing worker")

    try:
        executor = load_executor(settings.parsing_executor)
    except Exception as error:
        raise WorkerConfigurationError(
            f"Configured parser executor could not be loaded ({type(error).__name__})"
        ) from None
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop_event.set)

    worker = ParsingWorker(session_factory, executor, settings)
    logger.info("Parsing worker started")
    try:
        await worker.run(stop_event)
    finally:
        try:
            await close_executor(executor)
        finally:
            await dispose_database_engine()
            for signal_name in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signal_name)
            logger.info("Parsing worker stopped")


def main() -> None:
    """Start one independent worker process."""
    configure_logging(settings.log_level)
    try:
        asyncio.run(run_worker())
    except WorkerConfigurationError as error:
        logger.error("Parsing worker refused to start: %s", error)
        raise SystemExit(2) from error
    except Exception as error:
        logger.error(
            "Parsing worker stopped after runtime failure error_type=%s",
            type(error).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
