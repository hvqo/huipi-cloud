"""Assignment API integration tests backed by a dedicated PostgreSQL database."""

from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.main import app


@pytest.fixture
async def client(
    anyio_backend: str,
    postgres_engine: AsyncEngine,
) -> AsyncIterator[httpx.AsyncClient]:
    async with postgres_engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE assignments CASCADE"))

    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    async def override_session():
        async with session_factory() as session:
            yield session

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db_session] = override_session
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        async with postgres_engine.begin() as connection:
            await connection.execute(text("TRUNCATE TABLE assignments CASCADE"))


@pytest.mark.anyio
async def test_create_assignment(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/assignments",
        json={
            "title": "二次函数练习",
            "subject": "数学",
            "grade_level": "高一",
            "description": "第一章作业",
        },
    )

    assert response.status_code == 201
    assignment = response.json()
    assert assignment["status"] == "draft"
    assert assignment["questions"] == []
    assert assignment["title"] == "二次函数练习"


@pytest.mark.anyio
async def test_add_question(client: httpx.AsyncClient) -> None:
    assignment_id = await _create_assignment(client)

    response = await client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        json={
            "question_number": 1,
            "question_type": "short_answer",
            "stem": "求函数的顶点坐标。",
            "max_score": "10.00",
        },
    )

    assert response.status_code == 201
    question = response.json()
    assert question["question_number"] == 1
    assert Decimal(question["max_score"]) == Decimal("10.00")


@pytest.mark.anyio
async def test_set_answer_key(client: httpx.AsyncClient) -> None:
    question_id = await _create_question(client)

    response = await client.put(
        f"/api/v1/questions/{question_id}/answer-key",
        json={"answer_content": "顶点坐标为 (2, -1)。"},
    )

    assert response.status_code == 200
    answer_key = response.json()
    assert answer_key["answer_content"] == "顶点坐标为 (2, -1)。"
    assert answer_key["source"] == "teacher"


@pytest.mark.anyio
async def test_set_rubric(client: httpx.AsyncClient) -> None:
    question_id = await _create_question(client)

    response = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={
            "criteria": [
                {"description": "列出正确公式", "points": "4.50", "sort_order": 1},
                {"description": "结果正确", "points": "5.50", "sort_order": 2},
            ]
        },
    )

    assert response.status_code == 200
    criteria = response.json()["criteria"]
    assert len(criteria) == 2
    assert sum((Decimal(item["points"]) for item in criteria), Decimal("0.00")) == Decimal(
        "10.00"
    )


@pytest.mark.anyio
async def test_reject_invalid_rubric_points(client: httpx.AsyncClient) -> None:
    question_id = await _create_question(client)

    non_positive = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={"criteria": [{"description": "无效分值", "points": "0", "sort_order": 1}]},
    )
    exceeds_maximum = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={"criteria": [{"description": "超出满分", "points": "10.01", "sort_order": 1}]},
    )

    assert non_positive.status_code == 422
    assert exceeds_maximum.status_code == 422


@pytest.mark.anyio
async def test_refuse_to_publish_incomplete_assignment(client: httpx.AsyncClient) -> None:
    assignment_id = await _create_assignment(client)

    response = await client.post(f"/api/v1/assignments/{assignment_id}/publish")

    assert response.status_code == 409
    assert response.json()["detail"] == "作业至少需要一道题目才能发布"


@pytest.mark.anyio
async def test_refuse_to_publish_without_answer_key(client: httpx.AsyncClient) -> None:
    assignment_id = await _create_assignment(client)
    question_id = await _create_question(client, assignment_id=assignment_id)
    await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={"criteria": [{"description": "完整得分", "points": "10.00", "sort_order": 1}]},
    )

    response = await client.post(f"/api/v1/assignments/{assignment_id}/publish")

    assert response.status_code == 409
    assert response.json()["detail"] == "第 1 题尚未设置标准答案"


@pytest.mark.anyio
async def test_refuse_to_publish_when_rubric_total_is_not_max_score(
    client: httpx.AsyncClient,
) -> None:
    assignment_id = await _create_assignment(client)
    question_id = await _create_question(client, assignment_id=assignment_id)
    await client.put(
        f"/api/v1/questions/{question_id}/answer-key",
        json={"answer_content": "答案示例"},
    )
    await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={"criteria": [{"description": "部分得分", "points": "9.00", "sort_order": 1}]},
    )

    response = await client.post(f"/api/v1/assignments/{assignment_id}/publish")

    assert response.status_code == 409
    assert response.json()["detail"] == "第 1 题评分细则总分必须等于题目满分"


@pytest.mark.anyio
async def test_publish_complete_assignment(client: httpx.AsyncClient) -> None:
    assignment_id = await _create_assignment(client)
    question_id = await _create_question(client, assignment_id=assignment_id)
    await client.put(
        f"/api/v1/questions/{question_id}/answer-key",
        json={"answer_content": "顶点坐标为 (2, -1)。"},
    )
    await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={
            "criteria": [
                {"description": "公式与过程正确", "points": "6.00", "sort_order": 1},
                {"description": "答案正确", "points": "4.00", "sort_order": 2},
            ]
        },
    )

    response = await client.post(f"/api/v1/assignments/{assignment_id}/publish")

    assert response.status_code == 200
    published = response.json()
    assert published["status"] == "published"
    assert len(published["questions"]) == 1
    assert published["questions"][0]["answer_key"]["source"] == "teacher"


@pytest.mark.anyio
async def test_get_assignment_includes_its_question_answer_and_rubric(
    client: httpx.AsyncClient,
) -> None:
    assignment_id = await _create_assignment(client)
    question_id = await _create_question(client, assignment_id=assignment_id)
    await client.put(
        f"/api/v1/questions/{question_id}/answer-key",
        json={"answer_content": "答案示例"},
    )
    await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={
            "criteria": [
                {"description": "推导正确", "points": "10.00", "sort_order": 1},
            ]
        },
    )

    response = await client.get(f"/api/v1/assignments/{assignment_id}")

    assert response.status_code == 200
    question = response.json()["questions"][0]
    assert question["answer_key"]["answer_content"] == "答案示例"
    assert question["rubric_criteria"][0]["description"] == "推导正确"


@pytest.mark.anyio
async def test_list_assignments_returns_summaries(client: httpx.AsyncClient) -> None:
    assignment_id = await _create_assignment(client)

    response = await client.get("/api/v1/assignments")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [assignment_id]
    assert "questions" not in response.json()[0]


@pytest.mark.anyio
async def test_get_missing_assignment_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/assignments/00000000-0000-0000-0000-000000000001")

    assert response.status_code == 404
    assert response.json() == {"detail": "作业不存在"}


@pytest.mark.anyio
async def test_invalid_assignment_id_returns_422(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/assignments/not-a-uuid")

    assert response.status_code == 422


@pytest.mark.anyio
async def test_migration_is_applied_to_postgres(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        tables = set(
            (
                await connection.scalars(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = current_schema()"
                    )
                )
            ).all()
        )

    assert version == "14cbab704b41"
    assert {
        "assignments",
        "questions",
        "answer_keys",
        "rubric_criteria",
    }.issubset(tables)


async def _create_assignment(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/v1/assignments",
        json={"title": "二次函数练习", "subject": "数学", "grade_level": "高一"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _create_question(
    client: httpx.AsyncClient,
    *,
    assignment_id: str | None = None,
) -> str:
    assignment_id = assignment_id or await _create_assignment(client)
    response = await client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        json={
            "question_number": 1,
            "question_type": "short_answer",
            "stem": "求函数的顶点坐标。",
            "max_score": "10.00",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]
