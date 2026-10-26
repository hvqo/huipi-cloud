"""HTTP inventory. No new permission, role, or implicitly granted capability."""

PREFIX = "/exam-configuration"
# kind is a real parent resource; roster IDs are resolved to Session on the server.
POLICIES = {
    "configuration_options": ("GET", "/options", "list", ()),
    "configuration_events": ("GET", "/events", "list", ()),
    "configuration_sessions": ("GET", "/sessions", "list", ()),
    "configuration_papers": ("GET", "/papers", "list", ()),
    "configuration_create_event": ("POST", "/events", "installation", ("exam.event.create",)),
    "configuration_event": ("GET", "/events/{event_id}", "exam_event", ("exam.event.read",)),
    "configuration_event_state": (
        "POST",
        "/events/{event_id}/state",
        "exam_event",
        ("exam.event.manage",),
    ),
    "configuration_create_session": (
        "POST",
        "/events/{event_id}/sessions",
        "exam_event",
        ("exam.event.manage",),
    ),
    "configuration_session": (
        "GET",
        "/sessions/{session_id}",
        "exam_session",
        ("exam.session.read",),
    ),
    "configuration_session_state": (
        "POST",
        "/sessions/{session_id}/state",
        "exam_session",
        ("exam.session.manage",),
    ),
    "configuration_bind": (
        "POST",
        "/sessions/{session_id}/papers",
        "exam_session",
        ("exam.session.manage", "exam.session.paper.bind"),
    ),
    "configuration_unbind": (
        "POST",
        "/sessions/{session_id}/papers/revoke",
        "exam_session",
        ("exam.session.manage", "exam.session.paper.bind"),
    ),
    "configuration_candidates": (
        "POST",
        "/sessions/{session_id}/roster-preview",
        "exam_session",
        ("exam.roster.manage", "exam.roster.read"),
    ),
    "configuration_rosters": (
        "GET",
        "/sessions/{session_id}/rosters",
        "exam_session",
        ("exam.roster.read",),
    ),
    "configuration_create_roster": (
        "POST",
        "/sessions/{session_id}/rosters",
        "exam_session",
        ("exam.roster.manage",),
    ),
    "configuration_roster": ("GET", "/rosters/{roster_id}", "roster", ("exam.roster.read",)),
    "configuration_replace_roster": (
        "POST",
        "/rosters/{roster_id}/entries",
        "roster",
        ("exam.roster.manage",),
    ),
    "configuration_freeze_roster": (
        "POST",
        "/rosters/{roster_id}/freeze",
        "roster",
        ("exam.roster.freeze",),
    ),
    "configuration_correct_roster": (
        "POST",
        "/rosters/{roster_id}/corrections",
        "roster",
        ("exam.roster.manage", "exam.roster.correct"),
    ),
}
