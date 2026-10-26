TASK_POLICIES = {
    "list_marking_sessions": ("GET", "/marking-sessions", "list"),
    "list_my_marking_tasks": ("GET", "/my-marking-tasks", "list"),
    "list_marking_pool": ("GET", "/exam-sessions/{session_id}/marking-pool", "manager"),
    "list_marking_teachers": ("GET", "/exam-sessions/{session_id}/marking-teachers", "manager"),
    "inspect_marking_progress": ("GET", "/exam-sessions/{session_id}/marking-progress", "manager"),
    "prepare_marking_questions": (
        "POST",
        "/exam-sessions/{session_id}/marking-jobs/{job_id}/prepare",
        "manager",
    ),
    "assign_marking_tasks": ("POST", "/exam-sessions/{session_id}/marking-assignments", "manager"),
    "inspect_marking_task": ("GET", "/marking-tasks/{task_id}", "own"),
    "claim_marking_task": ("POST", "/marking-tasks/{task_id}/claim", "own"),
    "complete_marking_task": ("POST", "/marking-tasks/{task_id}/complete", "own"),
    "cancel_marking_task": ("POST", "/marking-tasks/{task_id}/cancel", "cancel"),
    "preview_marking_page": ("GET", "/marking-tasks/{task_id}/pages/{page_id}/preview", "own"),
}
