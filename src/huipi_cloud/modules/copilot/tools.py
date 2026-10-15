"""Fixed read-only business tools. Scope comes from the local operator, never the model."""

from __future__ import annotations

import json
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.infrastructure.embedding.protocol import EmbeddingError
from huipi_cloud.infrastructure.reranking.protocol import RerankError
from huipi_cloud.modules.answer_alignment.models import AnswerAlignmentArtifact
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.assignments import repository as assignment_repository
from huipi_cloud.modules.assignments.models import AnswerKey, Assignment, Question, RubricCriterion
from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact
from huipi_cloud.modules.copilot.protocol import CopilotError, StrictModel
from huipi_cloud.modules.knowledge.retriever import LexicalRetriever
from huipi_cloud.modules.knowledge.schemas import KnowledgeError
from huipi_cloud.modules.submissions.models import ParsingTask, Submission


class NoArgs(StrictModel):
    pass


class PageArgs(StrictModel):
    limit: int = Field(default=10, ge=1, le=25, strict=True)
    offset: int = Field(default=0, ge=0, le=10000, strict=True)


class QuestionArgs(StrictModel):
    question_id: UUID


class SubmissionArgs(StrictModel):
    submission_id: UUID


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=250)
    top_k: int = Field(default=3, ge=1, le=5, strict=True)


# A fixed allowlist; model text cannot add tools, endpoints, SQL, or Python.
REGISTRY = {
    "search_assignment_materials": (
        SearchArgs,
        "检索本作业当前有效的教师资料。回答须用原样[[citation_id]]引用；无命中不猜测。",
    ),
    "get_assignment_summary": (NoArgs, "读取作用域作业的名称、学科、状态及题目/提交数量。"),
    "list_assignment_questions": (PageArgs, "分页列出作用域作业题号、ID、类型和Rubric配置状态。"),
    "get_question_rubric_summary": (QuestionArgs, "读取作用域内题目的Rubric描述和分值上限。"),
    "get_submission_processing_status": (
        SubmissionArgs,
        "读取作用域内提交的处理索引状态；不是成绩。",
    ),
}


def tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema.model_json_schema(),
            },
        }
        for name, (schema, description) in REGISTRY.items()
    ]


def validate_arguments(name: str, arguments: str) -> StrictModel:
    if name not in REGISTRY:
        raise CopilotError("tool_not_allowed")
    try:
        # Reject duplicate keys too; never silently reinterpret ambiguous arguments.
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        parsed = json.loads(arguments, object_pairs_hook=unique_pairs)
        if not isinstance(parsed, dict) or len(arguments.encode()) > 2048:
            raise ValueError
        return REGISTRY[name][0].model_validate(parsed)
    except (ValidationError, ValueError, TypeError):
        raise CopilotError("invalid_tool_arguments") from None


class BusinessTools:
    def __init__(
        self,
        session_factory,
        assignment_id: UUID,
        *,
        storage=None,
        retriever=None,
        max_result_bytes=8192,
    ):
        self.session_factory = session_factory
        self.assignment_id = assignment_id
        self.retriever = retriever or LexicalRetriever(
            session_factory, storage, max_result_bytes=max_result_bytes
        )

    @property
    def retrieval_config(self):
        base = getattr(self.retriever, "source", self.retriever)
        dense = getattr(base, "dense", base)
        embedding = getattr(dense, "embedding", None)
        return {
            "implementation": type(self.retriever).__name__,
            "embedding_digest": embedding.identity.digest if embedding else None,
            "rerank_digest": getattr(
                getattr(self.retriever, "reranker", None), "identity", None
            ).digest
            if hasattr(self.retriever, "reranker")
            else None,
            "rerank_min_score": getattr(self.retriever, "min_score", None),
            "candidate_limit": getattr(self.retriever, "candidate_limit", None),
            "rerank_allow_fallback": getattr(self.retriever, "allow_fallback", False),
            "min_cosine": getattr(dense, "min_cosine", None),
            "allow_lexical_fallback": getattr(self.retriever, "allow_lexical_fallback", False),
        }

    async def validate_citations(self, hits):
        try:
            await self.retriever.validate_citations(self.assignment_id, hits)
        except (KnowledgeError, EmbeddingError, RerankError) as error:
            raise CopilotError(error.code) from None

    async def execute(self, name: str, args: StrictModel) -> dict:
        if name not in REGISTRY or not isinstance(args, REGISTRY[name][0]):
            raise CopilotError("tool_not_allowed")
        if self.session_factory is None:
            raise CopilotError("database_unavailable")
        try:
            async with self.session_factory() as session:
                async with session.begin():
                    # Also enforce read-only at PostgreSQL, not just in the prompt.
                    await session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    )
                    await session.execute(text("SET LOCAL statement_timeout = '3000ms'"))
                    found = await session.scalar(
                        select(Assignment.id).where(Assignment.id == self.assignment_id)
                    )
                    if found is None:
                        raise CopilotError("resource_not_found_in_scope")
                    if name != "search_assignment_materials":
                        return await self._read(session, name, args)
        except (SQLAlchemyError, OSError):
            raise CopilotError("database_unavailable") from None
        # Source I/O and lexical retrieval happen after the scope-check transaction closes.
        try:
            result = await self.retriever.search(self.assignment_id, args.query, args.top_k)
            return result.model_dump(mode="json")
        except (KnowledgeError, EmbeddingError, RerankError) as error:
            raise CopilotError(error.code) from None

    async def _read(self, session, name, args):
        aid = self.assignment_id
        if name == "get_assignment_summary":
            row = (
                await session.execute(
                    select(
                        Assignment.title,
                        Assignment.subject,
                        Assignment.grade_level,
                        Assignment.status,
                    ).where(Assignment.id == aid)
                )
            ).one()
            return {
                "assignment_id": str(aid),
                "title": row.title,
                "subject": row.subject,
                "grade_level": row.grade_level,
                "status": row.status,
                "question_count": await session.scalar(
                    select(func.count()).select_from(Question).where(Question.assignment_id == aid)
                ),
                "submission_count": await session.scalar(
                    select(func.count())
                    .select_from(Submission)
                    .where(Submission.assignment_id == aid)
                ),
                "published_grades": "not_implemented",
            }
        if name == "list_assignment_questions":
            answer_exists = (
                select(AnswerKey.id).where(AnswerKey.question_id == Question.id).exists()
            )
            count = (
                select(func.count())
                .select_from(RubricCriterion)
                .where(RubricCriterion.question_id == Question.id)
                .scalar_subquery()
            )
            rows = (
                await session.execute(
                    select(
                        Question.id,
                        Question.question_number,
                        Question.question_type,
                        Question.max_score,
                        answer_exists.label("answer_configured"),
                        count.label("rubric_count"),
                    )
                    .where(Question.assignment_id == aid)
                    .order_by(Question.question_number)
                    .limit(args.limit)
                    .offset(args.offset)
                )
            ).all()
            return {
                "items": [
                    {
                        "question_id": str(r.id),
                        "number": r.question_number,
                        "type": r.question_type,
                        "max_score": str(r.max_score),
                        "answer_configured": r.answer_configured,
                        "rubric_count": r.rubric_count,
                    }
                    for r in rows
                ],
                "limit": args.limit,
                "offset": args.offset,
                "next_offset": args.offset + len(rows) if len(rows) == args.limit else None,
            }
        if name == "get_question_rubric_summary":
            if (
                await assignment_repository.get_question_assignment_id(session, args.question_id)
                != aid
            ):
                raise CopilotError("resource_not_found_in_scope")
            rows = (
                await session.execute(
                    select(
                        RubricCriterion.description,
                        RubricCriterion.points,
                        RubricCriterion.sort_order,
                    )
                    .where(RubricCriterion.question_id == args.question_id)
                    .order_by(RubricCriterion.sort_order)
                    .limit(101)
                )
            ).all()
            return {
                "question_id": str(args.question_id),
                "criteria": [
                    {
                        "description": r.description[:300],
                        "description_truncated": len(r.description) > 300,
                        "points": str(r.points),
                        "sort_order": r.sort_order,
                    }
                    for r in rows[:100]
                ],
                "returned_criteria_count": min(len(rows), 100),
                "criteria_truncated": len(rows) > 100,
                "meaning": "rubric_maximum_points_not_student_grades",
            }
        sid = args.submission_id
        found = await session.scalar(
            select(Submission.id).where(Submission.id == sid, Submission.assignment_id == aid)
        )
        if found is None:
            raise CopilotError("resource_not_found_in_scope")
        parsing = await session.scalar(
            select(ParsingTask.status).where(ParsingTask.submission_id == sid)
        )
        canonical = (
            await session.execute(
                select(CanonicalArtifact.id, CanonicalArtifact.status)
                .where(CanonicalArtifact.submission_id == sid)
                .order_by(CanonicalArtifact.created_at.desc(), CanonicalArtifact.id)
                .limit(1)
            )
        ).first()
        alignment = (
            await session.execute(
                select(AnswerAlignmentArtifact.id, AnswerAlignmentArtifact.status)
                .where(AnswerAlignmentArtifact.submission_id == sid)
                .order_by(AnswerAlignmentArtifact.created_at.desc(), AnswerAlignmentArtifact.id)
                .limit(1)
            )
        ).first()
        # Latest per-question index decisions, deliberately not a readiness assessment.
        latest = (
            select(
                AnswerReviewDecision.decision,
                func.row_number()
                .over(
                    partition_by=AnswerReviewDecision.question_id,
                    order_by=AnswerReviewDecision.revision.desc(),
                )
                .label("rn"),
            )
            .where(AnswerReviewDecision.submission_id == sid)
            .subquery()
        )
        counts = (
            await session.execute(
                select(latest.c.decision, func.count())
                .where(latest.c.rn == 1)
                .group_by(latest.c.decision)
            )
        ).all()
        return {
            "submission_id": str(sid),
            "parsing_status": parsing or "not_registered",
            "canonical_index_status": canonical.status if canonical else "not_registered",
            "alignment_index_status": alignment.status if alignment else "not_registered",
            "latest_review_index_counts": dict(counts),
            "latest_human_answer_review_count": sum(count for _, count in counts),
            "human_review_count_meaning": (
                "latest_human_answer_presence_decisions_per_question_not_grade_reviews"
            ),
            "index_freshness": "not_verified_not_scoring_readiness",
            "published_grades": "not_implemented",
        }
