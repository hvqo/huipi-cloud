"""Explicit exam-result administration; class access remains independently required."""

from uuid import UUID

RESULT_ACTIONS = {
    code: frozenset({"installation", "exam_event", "exam_session"})
    for code in ("exam.result.manage", "exam.result.approve", "exam.result.publish")
}
RESULT_MANAGER_ROLE = UUID("11111111-b19a-4000-8000-000000000001")
RESULT_DEPLOYER_ROLE = UUID("11111111-b19a-4000-8000-000000000002")
