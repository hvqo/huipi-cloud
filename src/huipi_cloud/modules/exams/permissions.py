"""Executable actions only for real exam and paper entities."""

from uuid import UUID

EXAM_ACTIONS = {
    "exam.event.create": frozenset({"installation"}),
    "exam.event.read": frozenset({"installation", "exam_event"}),
    "exam.event.manage": frozenset({"installation", "exam_event"}),
    "exam.session.read": frozenset({"installation", "exam_event", "exam_session"}),
    "exam.session.manage": frozenset({"installation", "exam_event", "exam_session"}),
    "exam.session.paper.bind": frozenset({"installation", "exam_event", "exam_session"}),
    "paper.use": frozenset({"installation", "paper_version"}),
}
EXAM_OPERATOR_ROLE = UUID("11111111-b12b-4000-8000-000000000001")
EXAM_MANAGER_ROLE = UUID("11111111-b12b-4000-8000-000000000002")
SESSION_MANAGER_ROLE = UUID("11111111-b12b-4000-8000-000000000003")
PAPER_USER_ROLE = UUID("11111111-b12b-4000-8000-000000000004")
