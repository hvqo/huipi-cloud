"""Pure contracts for protected paper operations; no database or model runtime."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.papers.permissions import PAPER_ACTIONS
from huipi_cloud.modules.papers.schemas import QuestionEdit, Reorder, SnapshotCreate, VersionWrite
from huipi_cloud.modules.papers.service import validate_document


def base():
    return dict(
        request_id=uuid4(),
        paper_version_id=uuid4(),
        expected_revision=1,
        expected_content_sha256="a" * 64,
        expected_mapping_digest="b" * 64,
    )


@pytest.mark.parametrize(
    "claim", ["role", "is_admin", "security_classification", "scope", "actor_user_id"]
)
def test_untrusted_authority_fields_are_not_paper_requests(claim):
    with pytest.raises(ValidationError):
        SnapshotCreate(
            request_id=uuid4(), source_assignment_id=uuid4(), title="合成", **{claim: "forged"}
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_revision", 0),
        ("expected_content_sha256", "bad"),
        ("expected_mapping_digest", "BAD"),
        ("paper_version_id", "SELECT * FROM users"),
    ],
)
def test_optimistic_source_contract_rejects_invalid_values(field, value):
    value = dict(base(), **{field: value})
    with pytest.raises(ValidationError):
        VersionWrite(**value)


def test_paper_scope_is_real_type_not_old_assignment_or_future_session():
    assert PAPER_ACTIONS["paper.create"] == {"installation"}
    assert PAPER_ACTIONS["paper.read"] == {"installation", "paper_version"}
    assert all(
        "assignment" not in kinds and "exam_session" not in kinds
        for kinds in PAPER_ACTIONS.values()
    )


def test_reorder_is_whole_unique_mapping_set():
    key = uuid4()
    with pytest.raises(ValidationError):
        Reorder(
            **base(),
            mappings=[
                dict(question_key=key, printed_label="1"),
                dict(question_key=key, printed_label="2"),
            ],
        )
    with pytest.raises(ValidationError):
        Reorder(**base(), mappings=[])


def test_decimal_rubric_and_complete_content_not_synthetic_scoring():
    value = dict(
        **base(),
        question_key=uuid4(),
        question=dict(
            question_number=1, question_type="short_answer", stem="合成题", max_score="0.30"
        ),
        answer_content="合成答案",
        rubric=dict(
            criteria=[
                dict(description="第一项", points="0.10", sort_order=1),
                dict(description="第二项", points="0.20", sort_order=2),
            ]
        ),
    )
    assert str(QuestionEdit(**value).question.max_score) == "0.30"
    value["rubric"]["criteria"][1]["points"] = "0.19"
    with pytest.raises(ValidationError):
        QuestionEdit(**value)
    with pytest.raises(AccessError):
        validate_document({"questions": []})
    with pytest.raises(AccessError):
        validate_document({"questions": [dict(answer_key=None, rubric=[], max_score="1")]})
