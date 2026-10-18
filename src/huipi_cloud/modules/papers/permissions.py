"""Only real paper-version scopes; no future session or marking capability."""

from uuid import UUID

PAPER_ACTIONS = {
    "paper.create": frozenset({"installation"}),
    "paper.read": frozenset({"installation", "paper_version"}),
    "paper.edit": frozenset({"installation", "paper_version"}),
    "paper.lock": frozenset({"installation", "paper_version"}),
}
PAPER_OPERATOR_ROLE = UUID("11111111-b12a-4000-8000-000000000001")
PAPER_EDITOR_ROLE = UUID("11111111-b12a-4000-8000-000000000002")
PAPER_LOCKER_ROLE = UUID("11111111-b12a-4000-8000-000000000003")
PAPER_READER_ROLE = UUID("11111111-b12a-4000-8000-000000000004")
