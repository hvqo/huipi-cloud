"""Roster authority never follows legacy administrator or membership flags."""

from uuid import UUID

ROSTER_ACTIONS = {
    f"exam.roster.{action}": frozenset({"installation", "exam_event", "exam_session"})
    for action in ("read", "manage", "freeze", "correct")
}
ROSTER_OPERATOR_ROLE = UUID("11111111-b12c-4000-8000-000000000001")
ROSTER_MANAGER_ROLE = UUID("11111111-b12c-4000-8000-000000000002")
ROSTER_READER_ROLE = UUID("11111111-b12c-4000-8000-000000000003")
ROSTER_FREEZER_ROLE = UUID("11111111-b12c-4000-8000-000000000004")
