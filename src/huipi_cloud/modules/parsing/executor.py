"""Parser executor interface and immutable input metadata."""

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class ParsingInput:
    task_id: UUID
    submission_id: UUID
    bucket: str
    object_key: str
    content_type: str
    size_bytes: int
    sha256: str
    attempt_count: int


class ParserExecutor(Protocol):
    """Execute a real parser and return only after parsing has truly completed."""

    async def execute(self, task: ParsingInput) -> None:
        """Parse one submission; raise a typed error to control retry policy."""


ParserExecutorFactory = Callable[[], ParserExecutor]


def load_executor(import_path: str) -> ParserExecutor:
    """Load a configured production executor from ``module:factory``."""
    from importlib import import_module

    module_name, separator, attribute_name = import_path.partition(":")
    if not separator or not module_name or not attribute_name:
        raise ValueError("PARSING_EXECUTOR must use the module.path:factory format")
    factory = getattr(import_module(module_name), attribute_name, None)
    if not callable(factory):
        raise ValueError("PARSING_EXECUTOR must name a callable factory")
    executor = factory()
    execute = getattr(executor, "execute", None)
    if not callable(execute) or not inspect.iscoroutinefunction(execute):
        raise ValueError("Configured parser executor must provide async execute(task)")
    return executor


async def close_executor(executor: ParserExecutor) -> None:
    """Release optional parser resources without blocking the event loop."""
    async_closer = getattr(executor, "aclose", None)
    if callable(async_closer):
        result = async_closer()
        if inspect.isawaitable(result):
            await result
        return
    closer = getattr(executor, "close", None)
    if callable(closer):
        result = await asyncio.to_thread(closer)
        if inspect.isawaitable(result):
            await result
