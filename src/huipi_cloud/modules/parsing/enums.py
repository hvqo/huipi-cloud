"""Parsing task states and allowed state transitions."""

from enum import StrEnum


class ParsingTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


ALLOWED_TRANSITIONS: dict[ParsingTaskStatus, frozenset[ParsingTaskStatus]] = {
    ParsingTaskStatus.PENDING: frozenset({ParsingTaskStatus.RUNNING}),
    ParsingTaskStatus.RUNNING: frozenset(
        {
            ParsingTaskStatus.SUCCEEDED,
            ParsingTaskStatus.RETRY_WAIT,
            ParsingTaskStatus.FAILED,
        }
    ),
    ParsingTaskStatus.RETRY_WAIT: frozenset({ParsingTaskStatus.RUNNING}),
    ParsingTaskStatus.SUCCEEDED: frozenset(),
    ParsingTaskStatus.FAILED: frozenset(),
}


def can_transition(current: ParsingTaskStatus, target: ParsingTaskStatus) -> bool:
    """Return whether the documented finite-state machine allows the transition."""
    return target in ALLOWED_TRANSITIONS[current]
