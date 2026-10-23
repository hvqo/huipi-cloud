"""Explicit deployment-only protected worker, always using the existing real MinerU executor."""

import asyncio
import logging
import signal

from huipi_cloud.core.config import settings
from huipi_cloud.core.logging import configure_logging
from huipi_cloud.infrastructure.database.session import dispose_database_engine, session_factory
from huipi_cloud.infrastructure.parsing.mineru import build_mineru_executor
from huipi_cloud.modules.exam_parsing.runtime import ExamBackend, ProtectedExecutor
from huipi_cloud.workers.parsing import ParsingWorker


async def run():
    if session_factory is None:
        raise ValueError("DATABASE_URL is required")
    # Fixed assembly envelope: 75s maximum. A 180s lease also covers short DB transitions.
    config = settings.model_copy(
        update={
            "parsing_lease_seconds": 180,
            "max_upload_size_bytes": 32 * 1024**2,
            "mineru_max_pdf_pages": 100,
        }
    )
    parser = build_mineru_executor(config)
    backend = ExamBackend(parser.storage, config)
    executor = ProtectedExecutor(parser, backend)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await ParsingWorker(session_factory, executor, config, repository_backend=backend).run(stop)
    finally:
        await executor.aclose()
        await dispose_database_engine()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main():
    configure_logging(settings.log_level)
    try:
        asyncio.run(run())
    except Exception as error:
        # Never let SQL parameters, object keys or parser output become a CLI traceback.
        logging.getLogger(__name__).error(
            "Protected exam worker stopped error_type=%s", type(error).__name__
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
