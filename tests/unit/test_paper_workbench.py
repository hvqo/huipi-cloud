"""Pure web-contract validation; actual authorization is tested with PostgreSQL."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.papers.web_schemas import (
    PaperWebQuestionEdit,
    PaperWebReorder,
    PaperWebWrite,
)


def version():
    return dict(
        request_id=uuid4(),
        expected_revision=1,
        expected_content_sha256="a" * 64,
        expected_mapping_digest="b" * 64,
    )


def test_web_edit_reuses_exact_decimal_total_contract():
    body = dict(
        **version(),
        question_key=uuid4(),
        question=dict(
            question_number=1, question_type="short_answer", stem="合成题", max_score="0.30"
        ),
        answer_content="合成答案",
        rubric=dict(
            criteria=[
                dict(description="甲", points="0.10", sort_order=1),
                dict(description="乙", points="0.20", sort_order=2),
            ]
        ),
    )
    assert PaperWebQuestionEdit(**body).question.max_score.as_tuple().exponent == -2
    body["question"]["max_score"] = "0.31"
    with pytest.raises(ValidationError):
        PaperWebQuestionEdit(**body)


def test_web_write_does_not_accept_actor_scope_or_path_identity():
    for name, value in [
        ("role", "admin"),
        ("scope", "installation"),
        ("paper_version_id", uuid4()),
    ]:
        with pytest.raises(ValidationError):
            PaperWebWrite(**version(), **{name: value})


def test_reorder_needs_unique_keys_and_labels():
    key = uuid4()
    with pytest.raises(ValidationError):
        PaperWebReorder(**version(), mappings=[dict(question_key=key, printed_label="1")] * 2)


def test_each_write_carries_revision_and_both_source_digests():
    for key in ["expected_revision", "expected_content_sha256", "expected_mapping_digest"]:
        body = version()
        body.pop(key)
        with pytest.raises(ValidationError):
            PaperWebWrite(**body)
