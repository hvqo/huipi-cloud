from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from huipi_cloud.modules.exam_recovery import service
from huipi_cloud.modules.exam_recovery.policies import POLICIES
from huipi_cloud.modules.identity import dependencies


def test_all_recovery_routes_are_authenticated_fixed_read_operations():
    assert len(POLICIES) == 2
    for name, (method, path) in POLICIES.items():
        assert method == "GET"
        assert dependencies.POLICIES[name] == {"GET"}
        assert dependencies.POLICY_PATHS[name] == path


def test_projection_is_typed_bounded_and_never_returns_storage_or_student_answers():
    caps = dict.fromkeys(service.GROUPS, True) | {"upload": True}
    queries = service.queries(SimpleNamespace(user_id=uuid4()), uuid4(), caps)
    assert len(queries) == 6
    for query in queries:
        fields = set(query.selected_columns.keys())
        assert {"kind", "resource_id", "updated_at"} <= fields
        assert not {"object_key", "bucket", "student_name", "answer_content", "role"} & fields
        assert str(query.compile(dialect=postgresql.dialect()))


def test_partial_grants_are_not_combined_for_sensitive_exception_groups():
    sid, event = uuid4(), uuid4()
    scope = SimpleNamespace(kind="exam_session", exam_session_id=sid)
    grants = [(scope, {"exam.parse.read"}), (scope, {"exam.script.read", "exam.identity.verify"})]
    assert not service.allowed(grants, service.GROUPS["parsing"], dict(id=sid, exam_event_id=event))
    assert service.allowed(
        [(scope, service.GROUPS["parsing"])],
        service.GROUPS["parsing"],
        dict(id=sid, exam_event_id=event),
    )


def test_every_public_exception_has_a_defined_human_explanation():
    from typing import get_args

    from huipi_cloud.modules.exam_recovery.schemas import Kind

    assert set(get_args(Kind)) == set(service.DESCRIPTIONS)
    assert "缺考" in service.DESCRIPTIONS["identity_pending"][1]
    assert "零分" in service.DESCRIPTIONS["scoring_abstained"][1]
