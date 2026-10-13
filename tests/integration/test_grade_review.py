"""Real PostgreSQL/MinIO acceptance tests for append-only grading reviews."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from huipi_cloud.core.config import Settings
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.assignments.models import AnswerKey
from huipi_cloud.modules.grading import grade_review_repository
from huipi_cloud.modules.grading.grade_review_errors import (
    GradeReviewCommitOutcomeUnknownError,
    GradeReviewConflictError,
    GradeReviewIdempotencyConflictError,
    GradeReviewInvalidDecisionError,
    GradeReviewSourceStaleError,
)
from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision
from huipi_cloud.modules.grading.grade_review_protocol import (
    GRADE_REVIEW_SCHEMA_NAME,
    GRADE_REVIEW_SCHEMA_VERSION,
    GradeReviewDecisionCreate,
)
from huipi_cloud.modules.grading.grade_review_service import record_grade_review
from huipi_cloud.modules.grading.models import GradeInputArtifact, GradeProposalArtifact
from huipi_cloud.modules.grading.proposal_service import (
    grade_input_artifact,
    read_grade_proposal_artifact,
)
from huipi_cloud.modules.grading.service import read_grade_input_artifact
from huipi_cloud.workers import review_grade_proposal as review_cli

TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(TESTS_ROOT / "integration") not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT / "integration"))
from test_grade_proposals import _CandidateProvider, _ready_grade_input  # noqa: E402


def _review_payload(
    review_status: str = "accepted",
    *,
    reviewer_ref: str = "teacher.demo",
    comment: str | None = None,
    reason_codes: list[str] | None = None,
    criterion_reviews: list[dict] | None = None,
) -> GradeReviewDecisionCreate:
    return GradeReviewDecisionCreate.model_validate(
        {
            "schema_name": GRADE_REVIEW_SCHEMA_NAME,
            "schema_version": GRADE_REVIEW_SCHEMA_VERSION,
            "review_status": review_status,
            "reviewer_ref": reviewer_ref,
            "review_reason_codes": reason_codes or [],
            "review_comment": comment,
            "criterion_reviews": criterion_reviews or [],
        }
    )


async def _make_candidate(client, storage, engine, *, reply_override: dict | None = None):
    (
        factory,
        submission_id,
        question,
        answer_review,
        prepared,
        grade_input,
        reply,
    ) = await _ready_grade_input(client, storage, engine)
    if reply_override is not None:
        reply = reply_override
    provider = _CandidateProvider(engine, reply)
    result = await grade_input_artifact(
        factory,
        storage,
        grade_input_artifact_id=prepared.artifact_id,
        request_id=uuid4(),
        config=Settings(),
        provider=provider,
    )
    proposal, proposal_sha = await read_grade_proposal_artifact(
        factory,
        storage,
        result.proposal_id,
        config=Settings(),
    )
    grade_input = await read_grade_input_artifact(
        factory,
        storage,
        prepared.artifact_id,
        config=Settings(),
    )
    return (
        factory,
        submission_id,
        question,
        answer_review,
        prepared,
        grade_input,
        proposal,
        proposal_sha,
        result,
        provider,
    )


async def _read_bytes(storage, object_key: str) -> bytes:
    downloaded = await storage.download(object_key)
    return b"".join([chunk async for chunk in downloaded.chunks()])


@pytest.mark.anyio
async def test_pg_minio_accept_revise_history_keeps_sources_immutable_and_cli_redacted(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    tmp_path: Path,
    capsys,
) -> None:
    (
        factory,
        submission_id,
        question,
        answer_review,
        prepared,
        grade_input,
        proposal,
        proposal_sha,
        proposal_result,
        _provider,
    ) = await _make_candidate(submission_client, recording_minio_storage, postgres_engine)
    async with factory() as session:
        input_row = await session.get(GradeInputArtifact, prepared.artifact_id)
        proposal_row = await session.get(GradeProposalArtifact, proposal.proposal_id)
        decisions_before = list(
            (
                await session.scalars(
                    select(AnswerReviewDecision).where(
                        AnswerReviewDecision.submission_id == submission_id
                    )
                )
            ).all()
        )
    assert input_row is not None and proposal_row is not None
    input_bytes_before = await _read_bytes(recording_minio_storage, input_row.object_key)
    proposal_bytes_before = await _read_bytes(recording_minio_storage, proposal_row.object_key)
    review_snapshot_before = [
        (item.id, item.revision, item.request_sha256, item.decision, item.response_regions)
        for item in decisions_before
    ]

    accepted_file = tmp_path / "accepted.json"
    accepted_file.write_text(
        json.dumps(
            {
                "schema_name": GRADE_REVIEW_SCHEMA_NAME,
                "schema_version": GRADE_REVIEW_SCHEMA_VERSION,
                "review_status": "accepted",
                "reviewer_ref": "teacher.demo",
                "review_comment": "CLI内部评论不可输出。",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    accepted_request_id = uuid4()
    exit_code = await review_cli._run(
        argparse.Namespace(
            command="record",
            proposal_id=proposal.proposal_id,
            request_id=accepted_request_id,
            expected_revision=0,
            file=accepted_file,
        ),
        factory=factory,
        storage=recording_minio_storage,
    )
    assert exit_code == 0
    accepted_stdout = capsys.readouterr().out
    accepted_output = json.loads(accepted_stdout)
    assert accepted_output["review_status"] == "accepted"
    assert accepted_output["reviewed_total_score"] == str(proposal.total_score)
    assert accepted_output["changed_criterion_count"] == 0
    assert "CLI内部评论不可输出" not in accepted_stdout
    assert grade_input.selected_answer_segments[0].text not in accepted_stdout
    assert proposal.criterion_scores[0].rationale not in accepted_stdout

    inspect_exit = await review_cli._run(
        argparse.Namespace(command="inspect", proposal_id=proposal.proposal_id),
        factory=factory,
        storage=recording_minio_storage,
    )
    assert inspect_exit == 0
    inspect_stdout = capsys.readouterr().out
    inspected = json.loads(inspect_stdout)
    assert inspected["source_status"] == "current"
    assert inspected["latest_review_revision"] == 1
    assert inspected["latest_review_status"] == "accepted"
    assert inspected["reviewer_identity_authenticated"] is False
    assert inspected["grade_published"] is False
    assert grade_input.selected_answer_segments[0].text not in inspect_stdout
    assert proposal.criterion_scores[0].rationale not in inspect_stdout
    assert proposal_row.object_key not in inspect_stdout

    criterion_id = grade_input.rubric_snapshot[0].criterion_id
    segment = grade_input.selected_answer_segments[0]
    revised_payload = _review_payload(
        "revised",
        reviewer_ref="teacher.demo",
        comment="修订后保留的内部说明。",
        criterion_reviews=[
            {
                "criterion_id": str(criterion_id),
                "reviewed_points": "8.00",
                "rationale": "复核发现一个计算步骤不完整，按证据改为8分。",
                "evidence_references": [
                    {
                        "kind": "quote",
                        "segment_index": 0,
                        "text_start": 0,
                        "text_end": len(segment.text),
                        "quote": segment.text,
                    }
                ],
            }
        ],
    )
    revised_request_id = uuid4()
    revised = await record_grade_review(
        factory,
        recording_minio_storage,
        proposal_id=proposal.proposal_id,
        request_id=revised_request_id,
        expected_revision=1,
        payload=revised_payload,
        config=Settings(),
    )
    assert revised.created is True
    assert revised.review.review_revision == 2
    assert str(revised.review.supersedes_review_id) == accepted_output["review_id"]
    assert revised.review.proposed_total_score == proposal.total_score
    assert revised.review.reviewed_total_score == 8
    assert revised.review.criterion_reviews[0].changed is True
    assert revised.review.grade_input_sha256 == grade_input.input_sha256
    assert revised.review.proposal_sha256 == proposal_sha
    assert "grade_published" not in revised.review.model_dump(mode="json")

    replay = await record_grade_review(
        factory,
        recording_minio_storage,
        proposal_id=proposal.proposal_id,
        request_id=revised_request_id,
        expected_revision=1,
        payload=revised_payload,
        config=Settings(),
    )
    assert replay.created is False
    assert replay.review.review_id == revised.review.review_id

    with pytest.raises(GradeReviewIdempotencyConflictError):
        await record_grade_review(
            factory,
            recording_minio_storage,
            proposal_id=proposal.proposal_id,
            request_id=revised_request_id,
            expected_revision=1,
            payload=_review_payload(
                "revised",
                reviewer_ref="teacher.demo",
                comment="different request body",
                criterion_reviews=revised_payload.criterion_reviews[0:1]
                and [revised_payload.criterion_reviews[0].model_dump(mode="json")],
            ),
            config=Settings(),
        )

    history_exit = await review_cli._run(
        argparse.Namespace(command="history", proposal_id=proposal.proposal_id),
        factory=factory,
        storage=recording_minio_storage,
    )
    assert history_exit == 0
    history_stdout = capsys.readouterr().out
    history = json.loads(history_stdout)
    assert [item["review_revision"] for item in history] == [1, 2]
    assert history[1]["review_status"] == "revised"
    assert history[1]["criterion_scores"][0]["reviewed_points"] == "8.00"
    for secret in (
        grade_input.selected_answer_segments[0].text,
        proposal.criterion_scores[0].rationale,
        "修订后保留的内部说明。",
    ):
        assert secret not in history_stdout

    async with factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(GradeReviewRevision)
                    .where(GradeReviewRevision.proposal_id == proposal.proposal_id)
                    .order_by(GradeReviewRevision.review_revision)
                )
            ).all()
        )
        decisions_after = list(
            (
                await session.scalars(
                    select(AnswerReviewDecision).where(
                        AnswerReviewDecision.submission_id == submission_id
                    )
                )
            ).all()
        )
    assert len(rows) == 2
    assert rows[0].review_status == "accepted"
    assert rows[0].proposed_total_score == rows[0].reviewed_total_score
    assert rows[1].review_revision == 2
    assert rows[1].supersedes_review_id == rows[0].review_id
    assert rows[1].proposed_total_score == proposal.total_score
    assert rows[1].reviewed_total_score == 8
    assert [
        (item.id, item.revision, item.request_sha256, item.decision, item.response_regions)
        for item in decisions_after
    ] == review_snapshot_before
    assert answer_review.decision_id == decisions_after[0].id

    with pytest.raises(DBAPIError):
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE grade_review_revisions "
                    "SET reviewer_ref = 'tampered' WHERE review_id = :id"
                ),
                {"id": rows[0].review_id},
            )
    with pytest.raises(DBAPIError):
        async with factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM grade_review_revisions WHERE review_id = :id"),
                {"id": rows[0].review_id},
            )

    assert hashlib.sha256(input_bytes_before).hexdigest() == input_row.object_sha256
    assert hashlib.sha256(proposal_bytes_before).hexdigest() == proposal_row.object_sha256
    assert proposal_sha == proposal_row.object_sha256
    assert await _read_bytes(recording_minio_storage, input_row.object_key) == input_bytes_before
    assert (
        await _read_bytes(recording_minio_storage, proposal_row.object_key) == proposal_bytes_before
    )
    reread_proposal, reread_sha = await read_grade_proposal_artifact(
        factory,
        recording_minio_storage,
        proposal.proposal_id,
        config=Settings(),
    )
    reread_input = await read_grade_input_artifact(
        factory,
        recording_minio_storage,
        prepared.artifact_id,
        config=Settings(),
    )
    assert reread_sha == proposal_sha
    assert reread_proposal == proposal
    assert reread_input.input_sha256 == grade_input.input_sha256
    assert proposal_result.proposal_sha256 == proposal_sha


@pytest.mark.anyio
async def test_concurrent_reviews_with_same_expected_revision_only_append_one(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, _, _, _, _, _, proposal, _, _, _ = await _make_candidate(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    requests = [
        (
            uuid4(),
            _review_payload(
                "rejected",
                reviewer_ref=f"teacher.{index}",
                reason_codes=["candidate_not_reliable"],
            ),
        )
        for index in range(2)
    ]
    results = await asyncio.gather(
        *(
            record_grade_review(
                factory,
                recording_minio_storage,
                proposal_id=proposal.proposal_id,
                request_id=request_id,
                expected_revision=0,
                payload=payload,
                config=Settings(),
            )
            for request_id, payload in requests
        ),
        return_exceptions=True,
    )
    successes = [result for result in results if not isinstance(result, BaseException)]
    conflicts = [result for result in results if isinstance(result, GradeReviewConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    review = successes[0].review
    assert review.review_revision == 1
    assert review.review_status == "rejected"
    assert review.proposed_total_score == proposal.total_score
    assert review.reviewed_total_score is None
    async with factory() as session:
        count = len(
            (
                await session.scalars(
                    select(GradeReviewRevision).where(
                        GradeReviewRevision.proposal_id == proposal.proposal_id
                    )
                )
            ).all()
        )
    assert count == 1


@pytest.mark.anyio
async def test_changed_sources_reject_new_review_without_creating_history(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, _, question, _, _, _, proposal, _, _, _ = await _make_candidate(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    async with postgres_engine.begin() as connection:
        await connection.execute(
            AnswerKey.__table__.update()
            .where(AnswerKey.question_id == question.id)
            .values(answer_content="changed answer key")
        )
    with pytest.raises(GradeReviewSourceStaleError):
        await record_grade_review(
            factory,
            recording_minio_storage,
            proposal_id=proposal.proposal_id,
            request_id=uuid4(),
            expected_revision=0,
            payload=_review_payload(),
            config=Settings(),
        )
    async with factory() as session:
        count = len(
            (
                await session.scalars(
                    select(GradeReviewRevision).where(
                        GradeReviewRevision.proposal_id == proposal.proposal_id
                    )
                )
            ).all()
        )
    assert count == 0


@pytest.mark.anyio
async def test_proposal_object_hash_is_rechecked_in_the_write_transaction(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _, _, _, _, _, proposal, _, _, _ = await _make_candidate(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    append = grade_review_repository.append_review

    async def change_index_after_s3_preflight(*args, **kwargs):
        async with postgres_engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE grade_proposal_artifacts SET object_sha256 = :sha "
                    "WHERE id = :proposal_id"
                ),
                {"sha": "f" * 64, "proposal_id": proposal.proposal_id},
            )
        return await append(*args, **kwargs)

    monkeypatch.setattr(
        grade_review_repository,
        "append_review",
        change_index_after_s3_preflight,
    )
    with pytest.raises(GradeReviewSourceStaleError) as captured:
        await record_grade_review(
            factory,
            recording_minio_storage,
            proposal_id=proposal.proposal_id,
            request_id=uuid4(),
            expected_revision=0,
            payload=_review_payload(),
            config=Settings(),
        )
    assert captured.value.reason_code == "proposal_index_binding_changed"
    async with factory() as session:
        assert (
            await session.scalar(
                select(GradeReviewRevision.review_id).where(
                    GradeReviewRevision.proposal_id == proposal.proposal_id
                )
            )
            is None
        )


@pytest.mark.anyio
async def test_revision_score_bounds_and_quote_validation_reject_bad_input(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, _, _, _, _, grade_input, proposal, _, _, _ = await _make_candidate(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    criterion_id = grade_input.rubric_snapshot[0].criterion_id
    segment = grade_input.selected_answer_segments[0]

    def item(points: str, quote: str, start: int, end: int) -> dict:
        return {
            "criterion_id": str(criterion_id),
            "reviewed_points": points,
            "rationale": "复核后分数有变化，说明依据。",
            "evidence_references": [
                {
                    "kind": "quote",
                    "segment_index": 0,
                    "text_start": start,
                    "text_end": end,
                    "quote": quote,
                }
            ],
        }

    invalid_reviews = [
        [item("11.00", segment.text, 0, len(segment.text))],
        [item("8.00", "伪造引用", 0, 4)],
        [item("8.00", segment.text + "x", 0, len(segment.text) + 1)],
        [
            item("8.00", segment.text, 0, len(segment.text)),
            item("8.00", segment.text, 0, len(segment.text)),
        ],
    ]
    for criterion_reviews in invalid_reviews:
        with pytest.raises(GradeReviewInvalidDecisionError):
            await record_grade_review(
                factory,
                recording_minio_storage,
                proposal_id=proposal.proposal_id,
                request_id=uuid4(),
                expected_revision=0,
                payload=_review_payload("revised", criterion_reviews=criterion_reviews),
                config=Settings(),
            )
    async with factory() as session:
        count = len(
            (
                await session.scalars(
                    select(GradeReviewRevision).where(
                        GradeReviewRevision.proposal_id == proposal.proposal_id
                    )
                )
            ).all()
        )
    assert count == 0


@pytest.mark.anyio
async def test_source_change_after_cli_read_but_before_append_is_rejected(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    monkeypatch,
) -> None:
    factory, _, question, _, _, _, proposal, _, _, _ = await _make_candidate(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    append = grade_review_repository.append_review

    async def mutate_source_then_append(*args, **kwargs):
        async with postgres_engine.begin() as connection:
            await connection.execute(
                AnswerKey.__table__.update()
                .where(AnswerKey.question_id == question.id)
                .values(answer_content="changed after proposal verification")
            )
        return await append(*args, **kwargs)

    monkeypatch.setattr(grade_review_repository, "append_review", mutate_source_then_append)
    with pytest.raises(GradeReviewSourceStaleError):
        await record_grade_review(
            factory,
            recording_minio_storage,
            proposal_id=proposal.proposal_id,
            request_id=uuid4(),
            expected_revision=0,
            payload=_review_payload(),
            config=Settings(),
        )
    async with factory() as session:
        count = len(
            (
                await session.scalars(
                    select(GradeReviewRevision).where(
                        GradeReviewRevision.proposal_id == proposal.proposal_id
                    )
                )
            ).all()
        )
    assert count == 0


@pytest.mark.anyio
async def test_abstained_proposal_can_only_be_rejected_or_marked_for_regrading(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    abstain = {
        "schema_version": "1.0",
        "outcome": "abstain",
        "total_score": None,
        "criterion_scores": [],
        "abstention_reason": "证据不足，需要人工补充。",
    }
    factory, _, _, _, _, _, proposal, _, _, provider = await _make_candidate(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        reply_override=abstain,
    )
    assert proposal.grading_status == "grading_abstained"
    with pytest.raises(GradeReviewInvalidDecisionError) as captured:
        await record_grade_review(
            factory,
            recording_minio_storage,
            proposal_id=proposal.proposal_id,
            request_id=uuid4(),
            expected_revision=0,
            payload=_review_payload("accepted"),
            config=Settings(),
        )
    assert captured.value.reason_code == "abstained_proposal_cannot_be_scored"
    regrading = await record_grade_review(
        factory,
        recording_minio_storage,
        proposal_id=proposal.proposal_id,
        request_id=uuid4(),
        expected_revision=0,
        payload=_review_payload(
            "regrading_required",
            reason_codes=["additional_evidence_required"],
        ),
        config=Settings(),
    )
    assert regrading.review.review_status == "regrading_required"
    assert regrading.review.proposed_total_score is None
    assert regrading.review.reviewed_total_score is None
    assert provider.calls == 1


@pytest.mark.anyio
async def test_retry_after_lost_commit_acknowledgement_reads_the_existing_revision(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    monkeypatch,
) -> None:
    factory, _, _, _, _, _, proposal, _, _, _ = await _make_candidate(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    payload = _review_payload("rejected", reason_codes=["unsupported_score"])
    request_id = uuid4()
    append = grade_review_repository.append_review

    async def commit_then_lose_acknowledgement(*args, **kwargs):
        record, _ = await append(*args, **kwargs)
        raise GradeReviewCommitOutcomeUnknownError()

    monkeypatch.setattr(
        grade_review_repository,
        "append_review",
        commit_then_lose_acknowledgement,
    )
    with pytest.raises(GradeReviewCommitOutcomeUnknownError):
        await record_grade_review(
            factory,
            recording_minio_storage,
            proposal_id=proposal.proposal_id,
            request_id=request_id,
            expected_revision=0,
            payload=payload,
            config=Settings(),
        )
    monkeypatch.setattr(grade_review_repository, "append_review", append)
    retry = await record_grade_review(
        factory,
        recording_minio_storage,
        proposal_id=proposal.proposal_id,
        request_id=request_id,
        expected_revision=0,
        payload=payload,
        config=Settings(),
    )
    assert retry.created is False
    assert retry.review.review_revision == 1
