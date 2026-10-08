"""Assignment API integration tests backed by a dedicated PostgreSQL database."""

import asyncio
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.main import app
from huipi_cloud.modules.assignments import service as assignment_service


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
async def test_reject_non_positive_question_number(client: httpx.AsyncClient) -> None:
    assignment_id = await _create_assignment(client)
    response = await client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        json={
            "question_number": 0,
            "question_type": "short_answer",
            "stem": "题号必须为正数",
            "max_score": "10.00",
        },
    )

    assert response.status_code == 422
    detail = await client.get(f"/api/v1/assignments/{assignment_id}")
    assert detail.json()["questions"] == []


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
async def test_rubric_put_replaces_the_entire_list(client: httpx.AsyncClient) -> None:
    question_id = await _create_question(client)
    first = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={
            "criteria": [
                {"description": "过程", "points": "4.00", "sort_order": 1},
                {"description": "结果", "points": "6.00", "sort_order": 2},
            ]
        },
    )
    replacement_payload = {
        "criteria": [{"description": "新评分标准", "points": "10.00", "sort_order": 1}]
    }
    replacement = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json=replacement_payload,
    )
    repeated = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json=replacement_payload,
    )

    assert first.status_code == 200
    assert replacement.status_code == repeated.status_code == 200
    for response in (replacement, repeated):
        assert [
            (item["description"], Decimal(item["points"]), item["sort_order"])
            for item in response.json()["criteria"]
        ] == [("新评分标准", Decimal("10.00"), 1)]


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
async def test_reject_empty_and_duplicate_rubric_orders(client: httpx.AsyncClient) -> None:
    assignment_id = await _create_assignment(client)
    question_id = await _create_question(client, assignment_id=assignment_id)

    empty = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={"criteria": []},
    )
    duplicate_order = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={
            "criteria": [
                {"description": "第一项", "points": "5.00", "sort_order": 1},
                {"description": "第二项", "points": "5.00", "sort_order": 1},
            ]
        },
    )

    assert empty.status_code == 422
    assert duplicate_order.status_code == 422
    assignment = await client.get(f"/api/v1/assignments/{assignment_id}")
    assert assignment.status_code == 200
    assert assignment.json()["questions"][0]["rubric_criteria"] == []


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
async def test_published_assignment_rejects_all_content_updates(
    client: httpx.AsyncClient,
) -> None:
    assignment_id, question_id = await _create_complete_assignment(client)
    published = await client.post(f"/api/v1/assignments/{assignment_id}/publish")
    assert published.status_code == 200

    add_question = await client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        json={
            "question_number": 2,
            "question_type": "short_answer",
            "stem": "第二题",
            "max_score": "5.00",
        },
    )
    replace_answer = await client.put(
        f"/api/v1/questions/{question_id}/answer-key",
        json={"answer_content": "修改后的答案"},
    )
    replace_rubric = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={"criteria": [{"description": "修改后的规则", "points": "10.00", "sort_order": 1}]},
    )

    assert [add_question.status_code, replace_answer.status_code, replace_rubric.status_code] == [
        409,
        409,
        409,
    ]
    detail = await client.get(f"/api/v1/assignments/{assignment_id}")
    question = detail.json()["questions"][0]
    assert detail.json()["status"] == "published"
    assert len(detail.json()["questions"]) == 1
    assert question["answer_key"]["answer_content"] == "原标准答案"
    assert question["rubric_criteria"][0]["description"] == "原评分规则"


@pytest.mark.anyio
async def test_concurrent_duplicate_question_numbers_are_conflict_safe(
    client: httpx.AsyncClient,
) -> None:
    assignment_id = await _create_assignment(client)
    gate = asyncio.Barrier(3)

    async def add_duplicate() -> httpx.Response:
        await gate.wait()
        return await client.post(
            f"/api/v1/assignments/{assignment_id}/questions",
            json={
                "question_number": 1,
                "question_type": "short_answer",
                "stem": "相同题号并发测试",
                "max_score": "10.00",
            },
        )

    requests = [asyncio.create_task(add_duplicate()) for _ in range(2)]
    await gate.wait()
    responses = await asyncio.gather(*requests)

    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json() == {"detail": "该作业已存在相同题号"}
    detail = await client.get(f"/api/v1/assignments/{assignment_id}")
    assert [question["question_number"] for question in detail.json()["questions"]] == [1]


@pytest.mark.anyio
async def test_publish_and_answer_update_are_serialized(
    client: httpx.AsyncClient,
) -> None:
    assignment_id, question_id = await _create_complete_assignment(client)
    gate = asyncio.Barrier(3)

    async def publish() -> httpx.Response:
        await gate.wait()
        return await client.post(f"/api/v1/assignments/{assignment_id}/publish")

    async def update_answer() -> httpx.Response:
        await gate.wait()
        return await client.put(
            f"/api/v1/questions/{question_id}/answer-key",
            json={"answer_content": "并发更新后的答案"},
        )

    publish_task = asyncio.create_task(publish())
    update_task = asyncio.create_task(update_answer())
    await gate.wait()
    publish_response, update_response = await asyncio.gather(publish_task, update_task)

    assert publish_response.status_code == 200
    assert update_response.status_code in {200, 409}
    detail = await client.get(f"/api/v1/assignments/{assignment_id}")
    assert detail.json()["status"] == "published"
    answer = detail.json()["questions"][0]["answer_key"]["answer_content"]
    if update_response.status_code == 200:
        assert answer == "并发更新后的答案"
    else:
        assert answer == "原标准答案"


@pytest.mark.anyio
async def test_duplicate_number_integrity_error_is_safely_mapped(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment_id = await _create_assignment(client)
    await _create_question(client, assignment_id=assignment_id)

    async def stale_precheck(
        _session: AsyncSession,
        _assignment_id: UUID,
        _question_number: int,
    ) -> bool:
        return False

    monkeypatch.setattr(assignment_service.repository, "question_number_exists", stale_precheck)
    response = await client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        json={
            "question_number": 1,
            "question_type": "short_answer",
            "stem": "触发数据库唯一约束",
            "max_score": "10.00",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "该作业已存在相同题号"}
    assert "uq_questions_assignment_number" not in response.text
    detail = await client.get(f"/api/v1/assignments/{assignment_id}")
    assert len(detail.json()["questions"]) == 1


@pytest.mark.anyio
async def test_failed_rubric_replacement_rolls_back_all_changes(
    client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
) -> None:
    assignment_id, question_id = await _create_complete_assignment(client)
    await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={"criteria": [{"description": "原评分规则", "points": "10.00", "sort_order": 1}]},
    )
    async with postgres_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION reject_test_rubric_insert() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "IF NEW.description = '数据库故障注入' THEN "
                "RAISE EXCEPTION 'internal trigger secret marker'; END IF; "
                "RETURN NEW; END; $$"
            )
        )
        await connection.execute(
            text(
                "CREATE TRIGGER reject_test_rubric_insert BEFORE INSERT "
                "ON rubric_criteria FOR EACH ROW "
                "EXECUTE FUNCTION reject_test_rubric_insert()"
            )
        )

    try:
        response = await client.put(
            f"/api/v1/questions/{question_id}/rubric",
            json={
                "criteria": [
                    {"description": "新评分规则", "points": "4.00", "sort_order": 1},
                    {"description": "数据库故障注入", "points": "6.00", "sort_order": 2},
                ]
            },
        )
        assert response.status_code == 500
        assert response.json() == {"detail": "数据库操作失败，请稍后重试"}
        assert "internal trigger secret marker" not in response.text

        detail = await client.get(f"/api/v1/assignments/{assignment_id}")
        criteria = detail.json()["questions"][0]["rubric_criteria"]
        assert [(item["description"], Decimal(item["points"])) for item in criteria] == [
            ("原评分规则", Decimal("10.00"))
        ]
    finally:
        async with postgres_engine.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER IF EXISTS reject_test_rubric_insert ON rubric_criteria")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS reject_test_rubric_insert()"))


@pytest.mark.anyio
async def test_database_unavailable_returns_safe_503(client: httpx.AsyncClient) -> None:
    async def unavailable_session() -> None:
        raise OperationalError(
            "SELECT 1",
            {},
            RuntimeError("connection secret marker"),
        )

    app.dependency_overrides[get_db_session] = unavailable_session
    response = await client.get("/api/v1/assignments")

    assert response.status_code == 503
    assert response.json() == {"detail": "数据库暂时不可用，请稍后重试"}
    assert "connection secret marker" not in response.text


@pytest.mark.anyio
async def test_openapi_documents_domain_and_database_errors() -> None:
    responses = app.openapi()["paths"]["/api/v1/assignments/{assignment_id}/publish"]["post"][
        "responses"
    ]

    assert {"200", "404", "409", "422", "500", "503"}.issubset(responses)
    assert responses["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
    assert responses["503"]["description"] == "数据库暂时不可用"


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
        constraints = set(
            (
                await connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE connamespace = current_schema()::regnamespace"
                    )
                )
            ).all()
        )
        indexes = set(
            (
                await connection.scalars(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE schemaname = current_schema()"
                    )
                )
            ).all()
        )

    assert version == "fd1e8d651902"
    assert {
        "assignments",
        "questions",
        "answer_keys",
        "rubric_criteria",
        "submissions",
        "submission_files",
        "parsing_tasks",
    }.issubset(tables)
    assert {
        "uq_questions_assignment_number",
        "uq_answer_keys_question_id",
        "uq_rubric_criteria_question_order",
        "uq_submissions_assignment_student_ref",
        "uq_submission_files_bucket_object_key",
        "uq_parsing_tasks_submission_id",
        "ck_questions_number_positive",
        "ck_rubric_criteria_points_positive",
        "ck_submission_files_size_positive",
        "ck_parsing_tasks_status",
        "submissions_assignment_id_fkey",
        "submission_files_submission_id_fkey",
        "parsing_tasks_submission_id_fkey",
    }.issubset(constraints)
    assert {
        "ix_assignments_status_created_at",
        "ix_questions_assignment_id",
        "ix_rubric_criteria_question_id",
        "ix_submissions_assignment_created_at",
        "ix_parsing_tasks_status_created_at",
        "ix_submission_files_created_at",
    }.issubset(indexes)


async def _create_assignment(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/v1/assignments",
        json={"title": "二次函数练习", "subject": "数学", "grade_level": "高一"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _create_complete_assignment(client: httpx.AsyncClient) -> tuple[str, str]:
    assignment_id = await _create_assignment(client)
    question_id = await _create_question(client, assignment_id=assignment_id)
    answer_response = await client.put(
        f"/api/v1/questions/{question_id}/answer-key",
        json={"answer_content": "原标准答案"},
    )
    rubric_response = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={"criteria": [{"description": "原评分规则", "points": "10.00", "sort_order": 1}]},
    )
    assert answer_response.status_code == 200
    assert rubric_response.status_code == 200
    return assignment_id, question_id


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
