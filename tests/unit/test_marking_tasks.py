from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.identity import dependencies
from huipi_cloud.modules.marking_tasks.permissions import TASK_ACTIONS
from huipi_cloud.modules.marking_tasks.policies import TASK_POLICIES
from huipi_cloud.modules.marking_tasks.schemas import Assignment, TaskTarget, Transition


def test_explicit_actions_use_real_event_session_scopes_and_fixed_routes():
    assert TASK_ACTIONS == {
        k: frozenset({"installation", "exam_event", "exam_session"})
        for k in ["exam.task.manage", "exam.task.work"]
    }
    for name, (method, path, _) in TASK_POLICIES.items():
        assert dependencies.POLICIES[name] == {method}
        assert dependencies.POLICY_PATHS[name] == path


def test_task_assignment_cannot_accept_client_actor_or_permission_declarations():
    body = dict(
        request_id=uuid4(),
        assignee_user_id=uuid4(),
        targets=[TaskTarget(workspace_id=uuid4())],
        reason="单评",
    )
    assert not Assignment(**body).force
    for key in ["actor_user_id", "role", "include_protected", "scope"]:
        with pytest.raises(ValidationError):
            Assignment(**body, **{key: "admin"})


def test_request_payload_and_batch_are_bounded_and_revision_strict():
    with pytest.raises(ValidationError):
        Assignment(
            request_id=uuid4(),
            assignee_user_id=uuid4(),
            targets=[dict(workspace_id=uuid4())] * 101,
            reason="单评",
        )
    for value in [-1, False, "1"]:
        with pytest.raises(ValidationError):
            Transition(request_id=uuid4(), expected_revision=value)


def test_task_request_never_contains_scores_or_model_response():
    body = Transition(request_id=uuid4(), expected_revision=2)
    assert set(body.model_dump()) == {"request_id", "expected_revision", "reason"}
    with pytest.raises(ValidationError):
        Transition(**body.model_dump(), total_score="10.00")
