"""Real PG/S3 projection and completed-parser serialization regression."""

import pytest
from sqlalchemy import select
from test_answer_review import _prepare_review
from test_teacher_authorization import auth_api  # noqa: F401

from huipi_cloud.modules.assignments.models import RubricCriterion


@pytest.fixture
async def workbench_api(auth_api):  # noqa: F811
    from sqlalchemy import text

    try:
        yield auth_api
    finally:
        async with auth_api.factory() as session, session.begin():
            await session.execute(text("TRUNCATE assignments CASCADE"))


@pytest.mark.anyio
async def test_workbench_excludes_answers_enforces_scope_and_returns_completed_submission(
    workbench_api,
    postgres_engine,
):  # noqa: F811
    ctx = workbench_api
    ctx.client.headers.update(ctx.headers())
    _, sid, question, _, _, bundle = await _prepare_review(ctx.client, ctx.storage, postgres_engine)
    aid = bundle.context.assignment_id
    async with ctx.factory() as session, session.begin():
        for i in range(2, 9):
            session.add(
                RubricCriterion(
                    question_id=question.id, description=f"合成细则{i}", points="1", sort_order=i
                )
            )
    response = await ctx.client.get(f"/api/v1/assignments/{aid}/workbench")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["can_write"]
    q = data["questions"][0]
    assert q["rubric_count"] == 8 and q["rubric_truncated"] and len(q["rubric_criteria"]) == 5
    assert "answer_key" not in response.text and "answer_content" not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert (
        await ctx.client.get(f"/api/v1/assignments/{aid}/workbench", headers=ctx.headers("b"))
    ).status_code == 404
    grant = await ctx.client.put(
        f"/api/v1/assignments/{aid}/members/{ctx.users['reader'].id}",
        headers=ctx.headers("admin"),
        json={"can_write": False},
    )
    assert grant.status_code == 200
    assert not (
        await ctx.client.get(f"/api/v1/assignments/{aid}/workbench", headers=ctx.headers("reader"))
    ).json()["can_write"]
    for path in [f"/api/v1/submissions/{sid}", f"/api/v1/assignments/{aid}/submissions"]:
        result = await ctx.client.get(path)
        assert result.status_code == 200, result.text
        row = result.json()[0] if isinstance(result.json(), list) else result.json()
        assert row["parsing_task"]["status"] == "succeeded"
    assert (
        await ctx.client.get(f"/api/v1/assignments/{aid}/workbench?limit=26")
    ).status_code == 422
    async with ctx.factory() as session:
        assert len((await session.scalars(select(RubricCriterion))).all()) == 8


@pytest.mark.anyio
async def test_teacher_confirmed_coordinates_are_bounded_and_stale_sources_are_not_highlighted(
    workbench_api,
    postgres_engine,
):
    from uuid import uuid4

    from sqlalchemy import update

    from huipi_cloud.modules.answer_alignment import service as alignment
    from huipi_cloud.modules.assignments.models import Question

    ctx = workbench_api
    ctx.client.headers.update(ctx.headers())
    content = "第1题 学生作答：😀𠀀é 2+2=4。"
    _, sid, question, region, _, _ = await _prepare_review(
        ctx.client,
        ctx.storage,
        postgres_engine,
        content=content,
    )
    url = f"/api/v1/submissions/{sid}/answer-reviews/questions/{question.id}"
    view = (await ctx.client.get(url)).json()
    start = content.index("学生作答")
    selections = [
        dict(content_pointer=region.content_pointer, text_start=start, text_end=start + 2),
        dict(content_pointer=region.content_pointer, text_start=start + 2, text_end=len(content)),
    ]
    body = dict(
        request_id=str(uuid4()),
        expected_revision=0,
        expected_source=view["source"],
        decision="response_present",
        reason_codes=["student_work_visible"],
        response_regions=selections,
    )
    created = await ctx.client.post(url, json=body)
    assert created.status_code == 201, created.text
    first = (await ctx.client.get(url + "?confirmed_limit=1")).json()
    assert first["confirmed_truncated"] and first["next_confirmed_offset"] == 1
    assert first["confirmed_regions"][0] == dict(
        role="response_regions", page_number=1, **selections[0]
    )
    second = await ctx.client.get(url + "?confirmed_limit=1&confirmed_offset=1")
    assert second.json()["confirmed_regions"][0]["text_end"] == len(content)
    assert not second.json()["confirmed_truncated"]
    for private in ["object_key", "asset_refs", content]:
        assert private not in second.text
    assert (await ctx.client.get(url + "?confirmed_limit=51")).status_code == 422
    async with ctx.factory() as session, session.begin():
        await session.execute(
            update(Question).where(Question.id == question.id).values(question_type="essay")
        )
    await alignment.align_submission(ctx.factory, ctx.storage, sid)
    current = (await ctx.client.get(url)).json()
    assert not current["latest_source_matches"] and not current["confirmed_regions"]
    assert current["answer_presence"] == "unreviewed"
