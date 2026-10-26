from uuid import UUID

TASK_ACTIONS = {
    code: frozenset({"installation", "exam_event", "exam_session"})
    for code in ("exam.task.manage", "exam.task.work")
}
MANAGER_ROLE = UUID("11111111-b16a-4000-8000-000000000001")
TEACHER_ROLE = UUID("11111111-b16a-4000-8000-000000000002")
DEPLOYER_ROLE = UUID("11111111-b16a-4000-8000-000000000003")
