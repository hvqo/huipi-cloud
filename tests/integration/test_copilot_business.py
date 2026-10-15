"""Real PostgreSQL reads, S3-backed synthetic lifecycle, and explicitly paid cloud smoke."""

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.copilot.provider import DeepSeekCopilotProvider
from huipi_cloud.main import app
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.assignments.models import Assignment, Question, RubricCriterion
from huipi_cloud.modules.copilot.protocol import CopilotError, CopilotRequest, ModelReply, ToolCall
from huipi_cloud.modules.copilot.runtime import CopilotRuntime
from huipi_cloud.modules.copilot.tools import BusinessTools, validate_arguments
from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision
from huipi_cloud.modules.grading.models import GradeProposalArtifact
from huipi_cloud.modules.submissions.models import Submission

INTEGRATION = Path(__file__).resolve().parent
if str(INTEGRATION) not in sys.path:
    sys.path.insert(0, str(INTEGRATION))
from test_grade_input import _prepared_review  # noqa: E402


async def seeded(client, storage, engine):
    factory, sid, q, _ = await _prepared_review(client, storage, engine)
    async with factory() as session:
        aid = await session.scalar(select(Submission.assignment_id).where(Submission.id == sid))
    return factory, aid, sid, q.id


async def counts(factory):
    async with factory() as session:
        return [
            await session.scalar(select(func.count()).select_from(model))
            for model in (
                Assignment,
                Question,
                RubricCriterion,
                GradeProposalArtifact,
                GradeReviewRevision,
                AnswerReviewDecision,
            )
        ]


@pytest.mark.anyio
async def test_all_tools_read_real_pipeline_without_private_payload_or_writes(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    factory, aid, sid, qid = await seeded(
        submission_client, recording_minio_storage, postgres_engine
    )
    before = await counts(factory)
    tools = BusinessTools(factory, aid)
    requests = [
        ("get_assignment_summary", {}),
        ("list_assignment_questions", {}),
        ("get_question_rubric_summary", {"question_id": str(qid)}),
        ("get_submission_processing_status", {"submission_id": str(sid)}),
    ]
    results = []
    for name, args in requests:
        results.append(await tools.execute(name, validate_arguments(name, json.dumps(args))))
    assert results[0]["question_count"] == results[0]["submission_count"] == 1
    assert results[1]["items"][0]["question_id"] == str(qid)
    assert results[1]["items"][0]["answer_configured"] is True
    assert results[2]["criteria"][0]["points"] == "10.00"
    assert results[3]["parsing_status"] == "succeeded"
    assert results[3]["canonical_index_status"] == "available"
    assert results[3]["latest_review_index_counts"] == {"response_present": 1}
    assert results[3]["latest_human_answer_review_count"] == 1
    assert results[3]["index_freshness"] == "not_verified_not_scoring_readiness"
    serialized = json.dumps(results)
    for forbidden in ("object_key", "bucket", "student_ref", "answer_content", "学生作答"):
        assert forbidden not in serialized
    assert await counts(factory) == before
    assert not any("copilot" in path for path in app.openapi()["paths"])


@pytest.mark.anyio
async def test_cross_scope_questions_submissions_and_missing_ids_have_same_safe_error(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    factory, aid, sid, qid = await seeded(
        submission_client, recording_minio_storage, postgres_engine
    )
    other = Assignment(title="private-other-assignment", subject="数学", grade_level="高一")
    async with factory() as session, session.begin():
        session.add(other)
    tools = BusinessTools(factory, other.id)
    for name, key, foreign in [
        ("get_question_rubric_summary", "question_id", qid),
        ("get_submission_processing_status", "submission_id", sid),
    ]:
        for target in (foreign, uuid4()):
            with pytest.raises(CopilotError, match="resource_not_found_in_scope"):
                await tools.execute(name, validate_arguments(name, json.dumps({key: str(target)})))
    assert aid != other.id


@pytest.mark.anyio
async def test_postgres_enforces_read_only_and_rolls_back_tool_mutation(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    factory, aid, _, _ = await seeded(submission_client, recording_minio_storage, postgres_engine)
    async with factory() as session:
        before = await session.scalar(select(Assignment.title).where(Assignment.id == aid))

    class AccidentalWrite(BusinessTools):
        async def _read(self, session, name, args):
            await session.execute(
                update(Assignment)
                .where(Assignment.id == self.assignment_id)
                .values(title="must-not-write")
            )
            return {}

    with pytest.raises(CopilotError, match="database_unavailable"):
        await AccidentalWrite(factory, aid).execute(
            "get_assignment_summary", validate_arguments("get_assignment_summary", "{}")
        )
    async with factory() as session:
        assert await session.scalar(select(Assignment.title).where(Assignment.id == aid)) == before
    # Fresh tool session remains usable after the failed transaction.
    assert (
        await BusinessTools(factory, aid).execute(
            "get_assignment_summary", validate_arguments("get_assignment_summary", "{}")
        )
    )["title"] == before


@pytest.mark.anyio
async def test_real_unreachable_postgres_returns_safe_error():
    engine = create_async_engine(
        "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:1/huipi_copilot_test",
        connect_args={"timeout": 1},
    )
    try:
        with pytest.raises(CopilotError, match="database_unavailable"):
            await BusinessTools(async_sessionmaker(engine), uuid4()).execute(
                "get_assignment_summary", validate_arguments("get_assignment_summary", "{}")
            )
    finally:
        await engine.dispose()


class PipelineProvider:
    generation_config_digest = "b" * 64

    def __init__(self, sid, qid):
        self.sid, self.qid, self.calls = sid, qid, 0

    async def complete(self, messages, tools):
        self.calls += 1
        calls = []
        if self.calls == 1:
            calls = [
                ToolCall(id=f"c{i}", name=name, arguments=json.dumps(args))
                for i, (name, args) in enumerate(
                    [
                        ("get_assignment_summary", {}),
                        ("list_assignment_questions", {}),
                        ("get_question_rubric_summary", {"question_id": str(self.qid)}),
                        ("get_submission_processing_status", {"submission_id": str(self.sid)}),
                    ]
                )
            ]
            answer = ""
        else:
            values = [json.loads(m["content"])["data"] for m in messages if m["role"] == "tool"]
            assert values[0]["question_count"] == 1
            assert values[2]["criteria"][0]["points"] == "10.00"
            assert values[3]["latest_review_index_counts"] == {"response_present": 1}
            answer = "1道题；Rubric上限10分；有1道人工作答确认。系统没有正式发布成绩。"
        return ModelReply(
            model_id="synthetic-fake",
            content=answer,
            tool_calls=calls,
            prompt_tokens=100,
            completion_tokens=50,
        )


@pytest.mark.anyio
async def test_agent_real_business_pipeline_fake_model_is_read_only(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    factory, aid, sid, qid = await seeded(
        submission_client, recording_minio_storage, postgres_engine
    )
    before = await counts(factory)
    result = await CopilotRuntime(
        PipelineProvider(sid, qid), BusinessTools(factory, aid), Settings(_env_file=None)
    ).run(CopilotRequest(assignment_id=aid, question="查询作业、题目、细则和提交处理状态"))
    assert result.status == "completed" and result.tool_calls == 4 and result.model_calls == 2
    assert "没有正式发布成绩" in result.answer
    assert [e.status for e in result.trace if e.kind == "tool"] == ["ok"] * 4
    assert await counts(factory) == before


@pytest.mark.anyio
@pytest.mark.copilot_e2e
async def test_real_deepseek_tools_and_answer_opt_in(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    if os.getenv("RUN_COPILOT_E2E") != "1":
        pytest.skip("paid synthetic-only Copilot E2E requires RUN_COPILOT_E2E=1")
    if os.getenv("COPILOT_SYNTHETIC_TRANSFER_AUTHORIZED") != "1":
        pytest.fail("explicit synthetic cloud transfer authorization is required")
    cfg = Settings()
    key = cfg.copilot_api_key or cfg.grading_api_key
    if not key:
        pytest.fail("local DeepSeek key missing; never print the key")
    cfg = cfg.model_copy(
        update={
            "copilot_enabled": True,
            "copilot_api_key": key,
            "copilot_allow_remote": True,
            "copilot_external_data_authorized": True,
        }
    )
    factory, aid, sid, qid = await seeded(
        submission_client, recording_minio_storage, postgres_engine
    )
    before = await counts(factory)
    provider = DeepSeekCopilotProvider(cfg)
    runtime = CopilotRuntime(provider, BusinessTools(factory, aid), cfg)
    direct = await runtime.run(
        CopilotRequest(
            assignment_id=aid, question="不用查数据，简短解释模型候选评分为什么不等于正式成绩。"
        )
    )
    assert direct.status == "completed" and direct.tool_calls == 0
    result = await runtime.run(
        CopilotRequest(
            assignment_id=aid,
            question=(
                "这是已授权合成数据。请实际查询本作用域作业概要和题目列表，"
                f"查询题目{qid}的Rubric，以及提交{sid}的处理状态和人工确认数量。"
                "回答题目数、Rubric分值上限、解析状态和人工确认数量；不可称为正式成绩。"
            ),
        )
    )
    payload = {
        "synthetic_only": True,
        "direct": direct.model_dump(mode="json"),
        "business": result.model_dump(mode="json"),
    }
    # Local opt-in evidence only. Never write live results to business tables.
    output = os.getenv("COPILOT_E2E_REPORT")
    if output:
        Path(output).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    assert result.status == "completed", result.finish_reason
    assert {e.name for e in result.trace if e.kind == "tool"} == {
        "get_assignment_summary",
        "list_assignment_questions",
        "get_question_rubric_summary",
        "get_submission_processing_status",
    }
    assert all(e.status == "ok" for e in result.trace if e.kind == "tool")
    assert await counts(factory) == before
    assert "10" in result.answer and "1" in result.answer
