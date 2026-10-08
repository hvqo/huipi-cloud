"""Independent polling worker for PostgreSQL-backed parsing tasks."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.core.config import Settings
from huipi_cloud.modules.parsing import repository
from huipi_cloud.modules.parsing.errors import classify_failure
from huipi_cloud.modules.parsing.executor import ParserExecutor
from huipi_cloud.modules.parsing.service import retry_delay_seconds

logger = logging.getLogger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]


class ParsingWorker:
    """Claim, execute, renew, and finalize one task at a time per process."""

    def __init__(
        self,
        session_factory: SessionFactory,
        executor: ParserExecutor,
        config: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.executor = executor
        self.config = config

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll until shutdown, and stop taking work before the current lease."""
        while not stop_event.is_set():
            claimed = await repository.claim_next_task(
                self.session_factory,
                lease_seconds=self.config.parsing_lease_seconds,
            )
            if claimed is None:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self.config.parsing_poll_seconds,
                    )
                except TimeoutError:
                    pass
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
                        "Parsing worker shutdown grace expired; lease will recover task_id=%s",
                        claimed.task.task_id,
                    )
                    active_task.cancel()
                    await asyncio.gather(active_task, return_exceptions=True)
                return
            finally:
                stop_waiter.cancel()
                await asyncio.gather(stop_waiter, return_exceptions=True)

    async def _execute_claim(self, claimed: repository.ClaimedTask) -> None:
        execution = asyncio.create_task(self.executor.execute(claimed.task))
        try:
            while True:
                done, _ = await asyncio.wait(
                    {execution},
                    timeout=self.config.parsing_heartbeat_seconds,
                )
                if execution in done:
                    try:
                        await execution
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        await self._record_error(claimed, error)
                        return
                    completed = await repository.complete_task(
                        self.session_factory,
                        task_id=claimed.task.task_id,
                        lease_token=claimed.lease_token,
                    )
                    if completed:
                        logger.info("Parsing task completed task_id=%s", claimed.task.task_id)
                    else:
                        logger.warning(
                            "Parsing task completion ignored after lease loss task_id=%s",
                            claimed.task.task_id,
                        )
                    return

                renewed_until = await repository.heartbeat(
                    self.session_factory,
                    task_id=claimed.task.task_id,
                    lease_token=claimed.lease_token,
                    lease_seconds=self.config.parsing_lease_seconds,
                )
                if renewed_until is None:
                    logger.warning("Parsing lease lost task_id=%s", claimed.task.task_id)
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                    return
                logger.debug(
                    "Parsing lease renewed task_id=%s expires_at=%s",
                    claimed.task.task_id,
                    renewed_until.isoformat(),
                )
        except asyncio.CancelledError:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
            raise

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
