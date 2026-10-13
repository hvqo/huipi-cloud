"""PostgreSQL/MinIO checks for source-bound rubric proposal persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.grading.provider import ProviderUsage, TextGradingResponse
from huipi_cloud.infrastructure.storage.s3 import (
    StorageObjectNotFoundError,
    StorageUnavailableError,
)
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.assignments.models import AnswerKey, RubricCriterion
from huipi_cloud.modules.grading import proposal_repository
from huipi_cloud.modules.grading.models import GradeInputArtifact, GradeProposalArtifact
from huipi_cloud.modules.grading.proposal_errors import (
    GradeProposalCommitOutcomeUnknownError,
    GradeProposalIdempotencyConflictError,
    GradeProposalInputStaleError,
    GradeProposalInvalidEvidenceError,
    GradeProposalRegistrationRolledBackError,
)
from huipi_cloud.modules.grading.proposal_protocol import GradeProposalDocument
from huipi_cloud.modules.grading.proposal_service import (
    grade_input_artifact,
    read_grade_proposal_artifact,
)
from huipi_cloud.modules.grading.service import prepare_grade_input, read_grade_input_artifact

TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(TESTS_ROOT / "integration") not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT / "integration"))
from test_grade_input import _prepared_review  # noqa: E402


class _CandidateProvider:
    provider_name = "ollama_native"
    model_id = "synthetic-test-provider"
    generation_config_digest = hashlib.sha256(b"synthetic-test-config").hexdigest()

    def __init__(
        self, engine: AsyncEngine, reply: dict, *, barrier=None, callback=None, max_checked_out=0
    ):
        self.engine = engine
        self.reply = reply
        self.barrier = barrier
        self.callback = callback
        self.max_checked_out = max_checked_out
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, *, system_prompt: str, user_prompt: str) -> TextGradingResponse:
        self.calls += 1
        self.prompts.append(user_prompt)
        # During a concurrent request, the peer may still be in its short
        # pre-call read transaction. This assertion excludes held model-call
        # sessions while allowing that one independent read.
        assert self.engine.sync_engine.pool.checkedout() <= self.max_checked_out
        if self.barrier is not None:
            await asyncio.wait_for(self.barrier.wait(), timeout=10)
        if self.callback is not None:
            await self.callback()
        return TextGradingResponse(
            content=json.dumps(self.reply, ensure_ascii=False),
            model_id=self.model_id,
            elapsed_ms=17,
            usage=ProviderUsage(prompt_tokens=321, completion_tokens=45),
        )


async def _ready_grade_input(
    client,
    storage,
    engine,
    *,
    middle_content: str = "第1题 计算题干：2+2。学生作答：4。",
    response_text: str = "学生作答：4。",
    rubric_items: list[tuple[str, str]] | None = None,
):
    factory, submission_id, question, review = await _prepared_review(
        client,
        storage,
        engine,
        middle_content=middle_content,
        response_text=response_text,
    )
    async with engine.begin() as connection:
        await connection.execute(
            AnswerKey.__table__.update()
            .where(AnswerKey.question_id == question.id)
            .values(answer_content="4")
        )
        if rubric_items is None:
            await connection.execute(
                RubricCriterion.__table__.update()
                .where(RubricCriterion.question_id == question.id)
                .values(description="正确计算并给出答案 x=4")
            )
        else:
            await connection.execute(
                RubricCriterion.__table__.delete().where(
                    RubricCriterion.question_id == question.id
                )
            )
            await connection.execute(
                RubricCriterion.__table__.insert(),
                [
                    {
                        "question_id": question.id,
                        "description": description,
                        "points": points,
                        "sort_order": index,
                    }
                    for index, (description, points) in enumerate(rubric_items, start=1)
                ],
            )
    prepared = await prepare_grade_input(
        factory,
        storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
    )
    assert prepared.artifact_id is not None
    document = await read_grade_input_artifact(factory, storage, prepared.artifact_id)
    segment = document.selected_answer_segments[0]
    quote = segment.text
    criterion_scores = [
        {
            "criterion_id": str(criterion.criterion_id),
            "awarded_points": str(criterion.points),
            "rationale": "合成集成样例符合评分细则。",
            "evidence_references": [
                {
                    "kind": "quote",
                    "segment_index": 0,
                    "text_start": 0,
                    "text_end": len(quote),
                    "quote": quote,
                }
            ],
        }
        for criterion in document.rubric_snapshot
    ]
    reply = {
        "schema_version": "1.0",
        "outcome": "candidate",
        "total_score": str(
            sum((criterion.points for criterion in document.rubric_snapshot), Decimal("0.00"))
        ),
        "criterion_scores": criterion_scores,
    }
    return factory, submission_id, question, review, prepared, document, reply


@pytest.mark.anyio
async def test_real_postgres_minio_candidate_proposal_round_trip_and_human_review_is_unchanged(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    (
        factory,
        submission_id,
        question,
        review,
        prepared,
        document,
        reply,
    ) = await _ready_grade_input(submission_client, recording_minio_storage, postgres_engine)
    async with factory() as session:
        before_review = await session.scalar(
            select(func.count())
            .select_from(AnswerReviewDecision)
            .where(AnswerReviewDecision.submission_id == submission_id)
        )

    provider = _CandidateProvider(postgres_engine, reply)
    request_id = uuid4()
    result = await grade_input_artifact(
        factory,
        recording_minio_storage,
        grade_input_artifact_id=prepared.artifact_id,
        request_id=request_id,
        config=Settings(),
        provider=provider,
    )
    assert result.status == "completed_candidate"
    assert result.total_score == question.max_score
    assert result.max_score == question.max_score
    assert result.created is True
    assert result.prompt_tokens == 321
    assert result.completion_tokens == 45

    rows_before = set(recording_minio_storage.uploaded_keys)
    replay = await grade_input_artifact(
        factory,
        recording_minio_storage,
        grade_input_artifact_id=prepared.artifact_id,
        request_id=result.request_id,
        config=Settings(),
        provider=provider,
    )
    assert replay.created is False
    assert replay.proposal_id == result.proposal_id
    assert provider.calls == 1
    assert set(recording_minio_storage.uploaded_keys) == rows_before

    conflicting_provider = _CandidateProvider(postgres_engine, reply)
    conflicting_provider.model_id = "different-configured-model"
    with pytest.raises(GradeProposalIdempotencyConflictError):
        await grade_input_artifact(
            factory,
            recording_minio_storage,
            grade_input_artifact_id=prepared.artifact_id,
            request_id=request_id,
            config=Settings(),
            provider=conflicting_provider,
        )
    assert conflicting_provider.calls == 0

    proposal, digest = await read_grade_proposal_artifact(
        factory,
        recording_minio_storage,
        result.proposal_id,
        config=Settings(),
    )
    assert isinstance(proposal, GradeProposalDocument)
    assert digest == result.proposal_sha256
    assert proposal.grade_input_sha256 == document.input_sha256
    assert proposal.criterion_scores[0].evidence_references[0].quote == (
        document.selected_answer_segments[0].text
    )
    async with factory() as session:
        row = await proposal_repository.get_proposal(session, result.proposal_id)
        after_review = await session.scalar(
            select(func.count())
            .select_from(AnswerReviewDecision)
            .where(AnswerReviewDecision.submission_id == submission_id)
        )
        grade_input_count = await session.scalar(
            select(func.count())
            .select_from(GradeInputArtifact)
            .where(GradeInputArtifact.id == prepared.artifact_id)
        )
        proposal_count = await session.scalar(
            select(func.count())
            .select_from(GradeProposalArtifact)
            .where(GradeProposalArtifact.request_id == result.request_id)
        )
    assert row is not None
    assert row.bucket == recording_minio_storage.bucket
    assert row.object_sha256 == digest
    assert row.object_size_bytes > 0
    assert row.grading_status == "completed_candidate"
    assert after_review == before_review == 1
    assert grade_input_count == proposal_count == 1
    assert review.revision == 1

    body = b"".join(
        [chunk async for chunk in (await recording_minio_storage.download(row.object_key)).chunks()]
    )
    assert hashlib.sha256(body).hexdigest() == row.object_sha256
    assert len(body) == row.object_size_bytes
    assert document.selected_answer_segments[0].text in body.decode("utf-8")


@pytest.mark.anyio
async def test_positive_score_without_quote_is_rejected_before_proposal_storage(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, _, _, prepared, _, valid_reply = await _ready_grade_input(
        submission_client, recording_minio_storage, postgres_engine
    )
    invalid_reply = json.loads(json.dumps(valid_reply))
    invalid_reply["total_score"] = "5.00"
    invalid_reply["criterion_scores"][0]["awarded_points"] = "5.00"
    invalid_reply["criterion_scores"][0]["evidence_references"] = [
        {"kind": "no_evidence", "reason": "未见检验步骤"}
    ]
    uploaded_keys_before = set(recording_minio_storage.uploaded_keys)
    async with factory() as session:
        review_before = [
            (row.id, row.revision, row.decision)
            for row in (
                await session.scalars(
                    select(AnswerReviewDecision).where(
                        AnswerReviewDecision.submission_id == submission_id
                    )
                )
            ).all()
        ]

    provider = _CandidateProvider(postgres_engine, invalid_reply)
    with pytest.raises(GradeProposalInvalidEvidenceError) as captured:
        await grade_input_artifact(
            factory,
            recording_minio_storage,
            grade_input_artifact_id=prepared.artifact_id,
            request_id=uuid4(),
            config=Settings(),
            provider=provider,
        )

    assert captured.value.reason_code == "positive_score_without_quote"
    assert provider.calls == 1
    assert set(recording_minio_storage.uploaded_keys) == uploaded_keys_before
    assert not any(
        "/grading/proposals/" in key
        for key in set(recording_minio_storage.uploaded_keys) - uploaded_keys_before
    )
    async with factory() as session:
        proposal_count = await session.scalar(
            select(func.count())
            .select_from(GradeProposalArtifact)
            .where(GradeProposalArtifact.grade_input_artifact_id == prepared.artifact_id)
        )
        review_after = [
            (row.id, row.revision, row.decision)
            for row in (
                await session.scalars(
                    select(AnswerReviewDecision).where(
                        AnswerReviewDecision.submission_id == submission_id
                    )
                )
            ).all()
        ]
    assert proposal_count == 0
    assert review_after == review_before


@pytest.mark.anyio
async def test_provider_abstention_persists_no_candidate_scores(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, _, _, _, prepared, _, _ = await _ready_grade_input(
        submission_client, recording_minio_storage, postgres_engine
    )
    provider = _CandidateProvider(
        postgres_engine,
        {
            "schema_version": "1.0",
            "outcome": "abstain",
            "abstention_reason": "无法从当前文本确认评分依据。",
            "reason_codes": ["insufficient_evidence"],
            "total_score": None,
            "criterion_scores": [],
        },
    )

    result = await grade_input_artifact(
        factory,
        recording_minio_storage,
        grade_input_artifact_id=prepared.artifact_id,
        request_id=uuid4(),
        config=Settings(),
        provider=provider,
    )
    proposal, _ = await read_grade_proposal_artifact(
        factory,
        recording_minio_storage,
        result.proposal_id,
        config=Settings(),
    )

    assert result.status == "grading_abstained"
    assert result.total_score is None
    assert proposal.grading_status == "grading_abstained"
    assert proposal.criterion_scores == []
    assert proposal.total_score is None


@pytest.mark.anyio
async def test_concurrent_same_request_registers_one_index_and_keeps_one_object(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, _, _, _, prepared, _, reply = await _ready_grade_input(
        submission_client, recording_minio_storage, postgres_engine
    )
    request_id = uuid4()
    provider = _CandidateProvider(
        postgres_engine,
        reply,
        barrier=asyncio.Barrier(2),
        max_checked_out=1,
    )
    recording_minio_storage.after_upload_barrier = asyncio.Barrier(2)
    first, second = await asyncio.gather(
        *(
            grade_input_artifact(
                factory,
                recording_minio_storage,
                grade_input_artifact_id=prepared.artifact_id,
                request_id=request_id,
                config=Settings(),
                provider=provider,
            )
            for _ in range(2)
        )
    )
    recording_minio_storage.after_upload_barrier = None

    assert first.proposal_id == second.proposal_id
    assert {first.created, second.created} == {True, False}
    assert provider.calls == 2
    attempts = [
        key for key in recording_minio_storage.uploaded_keys if "/grading/proposals/" in key
    ]
    assert len(attempts) == 2
    async with factory() as session:
        rows = (
            await session.scalars(
                select(GradeProposalArtifact).where(GradeProposalArtifact.request_id == request_id)
            )
        ).all()
    assert len(rows) == 1
    live_objects = []
    for key in attempts:
        try:
            await recording_minio_storage.head_object(key)
        except StorageObjectNotFoundError:
            continue
        live_objects.append(key)
    assert live_objects == [rows[0].object_key]


@pytest.mark.anyio
async def test_distinct_request_ids_create_distinct_immutable_proposals(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, _, _, _, prepared, _, reply = await _ready_grade_input(
        submission_client, recording_minio_storage, postgres_engine
    )
    first = await grade_input_artifact(
        factory,
        recording_minio_storage,
        grade_input_artifact_id=prepared.artifact_id,
        request_id=uuid4(),
        config=Settings(),
        provider=_CandidateProvider(postgres_engine, reply),
    )
    second = await grade_input_artifact(
        factory,
        recording_minio_storage,
        grade_input_artifact_id=prepared.artifact_id,
        request_id=uuid4(),
        config=Settings(),
        provider=_CandidateProvider(postgres_engine, reply),
    )

    assert first.proposal_id != second.proposal_id
    assert first.request_id != second.request_id
    assert first.proposal_sha256 != second.proposal_sha256
    async with factory() as session:
        rows = (
            await session.scalars(
                select(GradeProposalArtifact).where(
                    GradeProposalArtifact.grade_input_artifact_id == prepared.artifact_id
                )
            )
        ).all()
    assert {row.id for row in rows} == {first.proposal_id, second.proposal_id}
    assert len({row.object_key for row in rows}) == 2


@pytest.mark.anyio
async def test_review_change_during_provider_call_blocks_persistence(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, _, prepared, _, reply = await _ready_grade_input(
        submission_client, recording_minio_storage, postgres_engine
    )
    from huipi_cloud.modules.answer_alignment import service as alignment_service
    from huipi_cloud.modules.answer_review import service as review_service
    from huipi_cloud.modules.answer_review.protocol import (
        AnswerReviewDecisionCreate,
        ReviewRegionSelection,
    )

    async def supersede_review():
        bundle = await alignment_service.load_verified_alignment_bundle(
            factory, recording_minio_storage, submission_id
        )
        node_text = bundle.canonical_document.pages[0].blocks[0].content.value
        start = node_text.index("学生作答：4。")
        region = ReviewRegionSelection(
            content_pointer="/pages/0/blocks/0/content",
            text_start=start,
            text_end=start + len("学生作答：4。"),
        )
        await review_service.record_decision(
            factory,
            recording_minio_storage,
            submission_id=submission_id,
            question_id=question.id,
            request_id=uuid4(),
            payload=AnswerReviewDecisionCreate(
                decision="uncertain",
                uncertain_regions=[region],
                reason_codes=["ambiguous_handwriting"],
                reviewer_ref="grade-proposal-integration",
            ),
        )

    provider = _CandidateProvider(postgres_engine, reply, callback=supersede_review)
    uploads_before = set(recording_minio_storage.uploaded_keys)
    with pytest.raises(GradeProposalInputStaleError):
        await grade_input_artifact(
            factory,
            recording_minio_storage,
            grade_input_artifact_id=prepared.artifact_id,
            request_id=uuid4(),
            config=Settings(),
            provider=provider,
        )
    assert provider.calls == 1
    assert set(recording_minio_storage.uploaded_keys) == uploads_before
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(GradeProposalArtifact))
    assert count == 0


@pytest.mark.anyio
async def test_database_failure_before_commit_rolls_back_and_deletes_proposal_object(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, _, _, _, prepared, _, reply = await _ready_grade_input(
        submission_client, recording_minio_storage, postgres_engine
    )

    def fail_proposal_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if "INSERT INTO grade_proposal_artifacts" in statement:
            raise OperationalError("insert", {}, RuntimeError("simulated test database error"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail_proposal_insert)
    try:
        with pytest.raises(GradeProposalRegistrationRolledBackError):
            await grade_input_artifact(
                factory,
                recording_minio_storage,
                grade_input_artifact_id=prepared.artifact_id,
                request_id=uuid4(),
                config=Settings(),
                provider=_CandidateProvider(postgres_engine, reply),
            )
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail_proposal_insert)

    attempts = [
        key for key in recording_minio_storage.uploaded_keys if "/grading/proposals/" in key
    ]
    assert len(attempts) == 1
    with pytest.raises(StorageObjectNotFoundError):
        await recording_minio_storage.head_object(attempts[0])
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(GradeProposalArtifact))
    assert count == 0


@pytest.mark.anyio
async def test_unknown_commit_preserves_object_and_request_can_replay(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _, _, _, prepared, _, reply = await _ready_grade_input(
        submission_client, recording_minio_storage, postgres_engine
    )
    request_id = uuid4()
    actual_register = proposal_repository.register_proposal

    async def commit_then_lose_confirmation(*args, **kwargs):
        await actual_register(*args, **kwargs)
        raise GradeProposalCommitOutcomeUnknownError

    monkeypatch.setattr(proposal_repository, "register_proposal", commit_then_lose_confirmation)
    with pytest.raises(GradeProposalCommitOutcomeUnknownError):
        await grade_input_artifact(
            factory,
            recording_minio_storage,
            grade_input_artifact_id=prepared.artifact_id,
            request_id=request_id,
            config=Settings(),
            provider=_CandidateProvider(postgres_engine, reply),
        )
    monkeypatch.setattr(proposal_repository, "register_proposal", actual_register)

    record = await proposal_repository.get_by_request_id(factory, request_id)
    assert record is not None
    await recording_minio_storage.head_object(record.object_key)
    replay = await grade_input_artifact(
        factory,
        recording_minio_storage,
        grade_input_artifact_id=prepared.artifact_id,
        request_id=request_id,
        config=Settings(),
        provider=_CandidateProvider(postgres_engine, reply),
    )
    assert replay.created is False
    assert replay.proposal_id == record.id


@pytest.mark.anyio
async def test_completed_object_upload_failure_does_not_register_success(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _, _, _, prepared, _, reply = await _ready_grade_input(
        submission_client, recording_minio_storage, postgres_engine
    )
    actual_upload = recording_minio_storage.upload_fileobj
    failed_keys: list[str] = []

    async def upload_then_raise(fileobj, key, content_type):
        await actual_upload(fileobj, key, content_type)
        if "/grading/proposals/" in key:
            failed_keys.append(key)
            raise StorageUnavailableError("simulated storage failure")

    monkeypatch.setattr(recording_minio_storage, "upload_fileobj", upload_then_raise)
    with pytest.raises(StorageUnavailableError):
        await grade_input_artifact(
            factory,
            recording_minio_storage,
            grade_input_artifact_id=prepared.artifact_id,
            request_id=uuid4(),
            config=Settings(),
            provider=_CandidateProvider(postgres_engine, reply),
        )
    assert len(failed_keys) == 1
    with pytest.raises(StorageObjectNotFoundError):
        await recording_minio_storage.head_object(failed_keys[0])
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(GradeProposalArtifact))
    assert count == 0


@pytest.mark.anyio
async def test_cancellation_during_proposal_upload_preserves_attempt_object_without_index(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _, _, _, prepared, _, reply = await _ready_grade_input(
        submission_client, recording_minio_storage, postgres_engine
    )
    actual_upload = recording_minio_storage.upload_fileobj
    upload_finished = asyncio.Event()
    hold_upload = asyncio.Event()

    async def upload_then_hold(fileobj, key, content_type):
        await actual_upload(fileobj, key, content_type)
        if "/grading/proposals/" in key:
            upload_finished.set()
            await hold_upload.wait()

    monkeypatch.setattr(recording_minio_storage, "upload_fileobj", upload_then_hold)
    task = asyncio.create_task(
        grade_input_artifact(
            factory,
            recording_minio_storage,
            grade_input_artifact_id=prepared.artifact_id,
            request_id=uuid4(),
            config=Settings(),
            provider=_CandidateProvider(postgres_engine, reply),
        )
    )
    await asyncio.wait_for(upload_finished.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    hold_upload.set()

    attempts = [
        key for key in recording_minio_storage.uploaded_keys if "/grading/proposals/" in key
    ]
    assert len(attempts) == 1
    await recording_minio_storage.head_object(attempts[0])
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(GradeProposalArtifact))
    assert count == 0


@pytest.mark.anyio
@pytest.mark.grade_llm_e2e
@pytest.mark.skipif(os.getenv("RUN_GRADE_LLM_E2E") != "1", reason="需要显式运行真实本地LLM验收")
async def test_real_ollama_postgres_minio_grade_input_to_proposal_e2e(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory, _, _, _, prepared, document, _ = await _ready_grade_input(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        middle_content="第1题 计算题干：2+2。4",
        response_text="4",
    )
    model = os.environ.get("GRADING_MODEL", "")
    assert model, "设置 GRADING_MODEL 为本机已安装的 Ollama 模型"
    from huipi_cloud.workers import grade_submission as grade_cli

    monkeypatch.setattr(grade_cli, "session_factory", factory)
    monkeypatch.setattr(grade_cli, "get_object_storage", lambda: recording_minio_storage)
    inspection_exit = await grade_cli._run(
        grade_cli._parser().parse_args(
            ["inspect", "--grade-input-id", str(prepared.artifact_id)]
        )
    )
    inspection = json.loads(capsys.readouterr().out)
    assert inspection_exit == 0
    assert inspection["status"] == "ready"
    assert inspection["model_id"] == model

    grading_exit = await grade_cli._run(
        grade_cli._parser().parse_args(
            [
                "grade",
                "--grade-input-id",
                str(prepared.artifact_id),
                "--request-id",
                str(uuid4()),
            ]
        )
    )
    result_data = json.loads(capsys.readouterr().out)
    assert grading_exit == 0, result_data
    from huipi_cloud.modules.grading.proposal_protocol import GradeProposalRunResult

    result = GradeProposalRunResult.model_validate(result_data)
    assert result.status in {"completed_candidate", "grading_abstained"}
    proposal, digest = await read_grade_proposal_artifact(
        factory,
        recording_minio_storage,
        result.proposal_id,
    )
    assert hashlib.sha256(
        (await _download_proposal(recording_minio_storage, result.proposal_id, factory)).encode()
    ).hexdigest() == digest
    async with factory() as session:
        indexed = await proposal_repository.get_proposal(session, result.proposal_id)
        review_count = await session.scalar(
            select(func.count())
            .select_from(AnswerReviewDecision)
            .where(AnswerReviewDecision.submission_id == indexed.submission_id)
        )
    assert indexed is not None
    assert review_count == 1
    evidence_summary = []
    if proposal.grading_status == "completed_candidate":
        for item in proposal.criterion_scores:
            for reference in item.evidence_references:
                if reference.kind == "quote":
                    assert reference.segment_index < len(document.selected_answer_segments)
                    segment = document.selected_answer_segments[reference.segment_index]
                    assert (
                        segment.text[reference.text_start : reference.text_end]
                        == reference.quote
                    )
                    evidence_summary.append(
                        {
                            "criterion_id": str(item.criterion_id),
                            "segment_index": reference.segment_index,
                            "range": [reference.text_start, reference.text_end],
                            "quote_characters": len(reference.quote),
                        }
                    )
    print(
        json.dumps(
            {
                "model": proposal.model_id,
                "status": result.status,
                "criterion_scores": [str(score) for _, score in result.criterion_scores],
                "evidence": evidence_summary,
                "total_score": str(result.total_score) if result.total_score is not None else None,
                "max_score": str(result.max_score),
                "proposal_sha256": digest,
                "latency_ms": result.model_elapsed_ms,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            },
            ensure_ascii=False,
        )
    )


async def _download_proposal(storage, proposal_id: UUID, session_factory) -> str:
    async with session_factory() as session:
        record = await proposal_repository.get_proposal(session, proposal_id)
    body = b"".join([chunk async for chunk in (await storage.download(record.object_key)).chunks()])
    assert len(body) == record.object_size_bytes
    return body.decode("utf-8")
