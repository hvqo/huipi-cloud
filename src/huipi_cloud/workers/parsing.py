"""Independent polling worker for PostgreSQL-backed parsing tasks."""

import asyncio
import logging
import os
from collections.abc import Callable
from typing import NoReturn
from uuid import UUID

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.core.config import Settings
from huipi_cloud.modules.parsing import repository
from huipi_cloud.modules.parsing.errors import (
    PermanentParsingError,
    RetryableParsingError,
    WorkerFatalParsingError,
    classify_failure,
)
from huipi_cloud.modules.parsing.executor import ParsedArtifactResult, ParserExecutor
from huipi_cloud.modules.parsing.service import retry_delay_seconds

logger = logging.getLogger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]


class UnresponsiveParserError(RuntimeError):
    """Raised when a parser does not stop within the configured cancellation grace."""


def _is_transient_database_error(error: BaseException) -> bool:
    """Recognize connection, operational, and pool timeout failures as transient."""
    return isinstance(error, (OperationalError, InterfaceError, SATimeoutError)) or (
        isinstance(error, DBAPIError) and error.connection_invalidated
    )


def _abort_process_for_unresponsive_parser(task_id: UUID) -> NoReturn:
    """Fail-stop this worker process when in-process cancellation cannot stop a parser."""
    logger.critical(
        "Parser ignored cancellation; terminating worker process task_id=%s",
        task_id,
    )
    logging.shutdown()
    os._exit(70)


class ParsingWorker:
    """Claim, execute, renew, and finalize one task at a time per process."""

    def __init__(
        self,
        session_factory: SessionFactory,
        executor: ParserExecutor,
        config: Settings,
        *,
        abort_process: Callable[[UUID], None] = _abort_process_for_unresponsive_parser,
    ) -> None:
        self.session_factory = session_factory
        self.executor = executor
        self.config = config
        self.abort_process = abort_process

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll until shutdown, and stop taking work before the current lease."""
        while not stop_event.is_set():
            try:
                claimed = await repository.claim_next_task(
                    self.session_factory,
                    lease_seconds=self.config.parsing_lease_seconds,
                )
            except (DBAPIError, SATimeoutError) as error:
                if not _is_transient_database_error(error):
                    raise
                logger.warning(
                    "Parsing task claim deferred after database failure error_type=%s",
                    type(error).__name__,
                )
                await self._wait_for_poll(stop_event)
                continue
            if claimed is None:
                await self._wait_for_poll(stop_event)
                continue

            logger.info(
                "Parsing task claimed task_id=%s attempt=%d",
                claimed.task.task_id,
                claimed.task.attempt_count,
            )
            active_task = asyncio.create_task(self._execute_claim(claimed))
            stop_waiter = asyncio.create_task(stop_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {active_task, stop_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if active_task in done:
                    await active_task
                    continue
                try:
                    await asyncio.wait_for(
                        asyncio.shield(active_task),
                        timeout=self.config.parsing_shutdown_grace_seconds,
                    )
                except TimeoutError:
                    logger.warning(
                        "Parsing shutdown grace expired; requesting cancellation task_id=%s",
                        claimed.task.task_id,
                    )
                    active_task.cancel()
                    bounded_wait = max(0.1, self.config.parsing_cancel_grace_seconds * 2)
                    stopped, _ = await asyncio.wait({active_task}, timeout=bounded_wait)
                    if not stopped:
                        self.abort_process(claimed.task.task_id)
                        raise UnresponsiveParserError(
                            "worker execution did not stop within the cancellation grace"
                        )
                    try:
                        await active_task
                    except asyncio.CancelledError:
                        pass
                return
            finally:
                stop_waiter.cancel()
                await asyncio.gather(stop_waiter, return_exceptions=True)

    async def _wait_for_poll(self, stop_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=self.config.parsing_poll_seconds,
            )
        except TimeoutError:
            pass

    async def _execute_claim(self, claimed: repository.ClaimedTask) -> None:
        execution = asyncio.create_task(self.executor.execute(claimed.task))
        deadline = (
            asyncio.get_running_loop().time()
            + self.config.parsing_execution_timeout_seconds
        )
        try:
            while True:
                loop = asyncio.get_running_loop()
                remaining = max(0.0, deadline - loop.time())
                done, _ = await asyncio.wait(
                    {execution},
                    timeout=max(
                        0.0,
                        min(self.config.parsing_heartbeat_seconds, remaining),
                    ),
                )
                if execution in done:
                    try:
                        artifact = await execution
                    except asyncio.CancelledError:
                        raise
                    except WorkerFatalParsingError:
                        raise
                    except Exception as error:
                        try:
                            await self._record_error(claimed, error)
                        except (DBAPIError, SATimeoutError) as database_error:
                            if not _is_transient_database_error(database_error):
                                raise
                            logger.warning(
                                "Parsing failure state was not confirmed error_type=%s task_id=%s",
                                type(database_error).__name__,
                                claimed.task.task_id,
                            )
                        return
                    if (
                        not isinstance(artifact, ParsedArtifactResult)
                        or artifact.task_id != claimed.task.task_id
                        or artifact.submission_id != claimed.task.submission_id
                        or artifact.original_sha256 != claimed.task.sha256
                        or artifact.bucket != claimed.task.bucket
                    ):
                        await self._record_error(
                            claimed,
                            PermanentParsingError("invalid_result"),
                        )
                        return
                    try:
                        completed = await repository.complete_task_with_artifact(
                            self.session_factory,
                            artifact=artifact,
                            lease_token=claimed.lease_token,
                        )
                    except (DBAPIError, SATimeoutError) as error:
                        if not _is_transient_database_error(error):
                            raise
                        logger.warning(
                            "Parsing completion was not confirmed error_type=%s task_id=%s",
                            type(error).__name__,
                            claimed.task.task_id,
                        )
                        return
                    if completed:
                        logger.info("Parsing task completed task_id=%s", claimed.task.task_id)
                    else:
                        logger.warning(
                            "Parsing task completion ignored after lease loss task_id=%s",
                            claimed.task.task_id,
                        )
                    return

                if asyncio.get_running_loop().time() >= deadline:
                    await self._cancel_execution(execution, claimed.task.task_id)
                    await self._record_timeout(claimed)
                    return

                try:
                    renewed_until = await repository.heartbeat(
                        self.session_factory,
                        task_id=claimed.task.task_id,
                        lease_token=claimed.lease_token,
                        lease_seconds=self.config.parsing_lease_seconds,
                    )
                except (DBAPIError, SATimeoutError) as error:
                    if not _is_transient_database_error(error):
                        raise
                    logger.warning(
                        "Parsing heartbeat failed; execution ownership is unknown "
                        "error_type=%s task_id=%s",
                        type(error).__name__,
                        claimed.task.task_id,
                    )
                    await self._cancel_execution(execution, claimed.task.task_id)
                    return
                if renewed_until is None:
                    logger.warning("Parsing lease lost task_id=%s", claimed.task.task_id)
                    await self._cancel_execution(execution, claimed.task.task_id)
                    return
                logger.debug(
                    "Parsing lease renewed task_id=%s expires_at=%s",
                    claimed.task.task_id,
                    renewed_until.isoformat(),
                )
        except UnresponsiveParserError:
            raise
        except asyncio.CancelledError:
            await self._cancel_execution(execution, claimed.task.task_id)
            raise
        except BaseException:
            if not execution.done():
                await self._cancel_execution(execution, claimed.task.task_id)
            raise

    async def _cancel_execution(self, execution: asyncio.Task[None], task_id: UUID) -> None:
        """Request parser cancellation and fail-stop if it does not cooperate promptly."""
        if execution.done():
            await asyncio.gather(execution, return_exceptions=True)
            return
        execution.cancel()
        done, _ = await asyncio.wait(
            {execution},
            timeout=self.config.parsing_cancel_grace_seconds,
        )
        if execution in done:
            await asyncio.gather(execution, return_exceptions=True)
            return
        self.abort_process(task_id)
        raise UnresponsiveParserError("parser did not stop within the cancellation grace")

    async def _record_error(
        self,
        claimed: repository.ClaimedTask,
        error: BaseException,
    ) -> None:
        failure = classify_failure(error)
        delay = retry_delay_seconds(
            claimed.task.attempt_count,
            self.config.parsing_retry_base_seconds,
            self.config.parsing_retry_max_seconds,
        )
        new_status = await repository.record_failure(
            self.session_factory,
            task_id=claimed.task.task_id,
            lease_token=claimed.lease_token,
            failure=failure,
            retry_delay_seconds=delay,
        )
        if new_status is None:
            logger.warning(
                "Parsing failure ignored after lease loss task_id=%s code=%s",
                claimed.task.task_id,
                failure.code,
            )
            return
        logger.warning(
            "Parsing task failed task_id=%s status=%s code=%s",
            claimed.task.task_id,
            new_status.value,
            failure.code,
        )

    async def _record_timeout(self, claimed: repository.ClaimedTask) -> None:
        """Record a retryable timeout after the parser has stopped cooperatively."""
        try:
            await self._record_error(
                claimed,
                RetryableParsingError("execution_timeout"),
            )
        except (DBAPIError, SATimeoutError) as error:
            if not _is_transient_database_error(error):
                raise
            logger.warning(
                "Parsing timeout state was not confirmed error_type=%s task_id=%s",
                type(error).__name__,
                claimed.task.task_id,
            )
