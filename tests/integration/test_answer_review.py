"""PostgreSQL and real MinIO integration coverage for local answer review."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.answer_alignment import service as alignment_service
from huipi_cloud.modules.answer_alignment.models import AnswerAlignmentArtifact
from huipi_cloud.modules.answer_review import repository as review_repository
from huipi_cloud.modules.answer_review import service as review_service
from huipi_cloud.modules.answer_review.errors import (
    AnswerReviewIdempotencyConflictError,
    AnswerReviewRegionInvalidError,
    AnswerReviewSourceChangedError,
)
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.answer_review.protocol import AnswerReviewDecisionCreate
from huipi_cloud.modules.assignments.models import Question
from huipi_cloud.modules.canonical_documents import service as canonical_service
from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "integration") not in sys.path:
    sys.path.insert(0, str(ROOT / "integration"))
from test_canonical_documents import _seed_parsed_source  # noqa: E402


async def _prepare_review(
    client: httpx.AsyncClient,
    storage,
    engine: AsyncEngine,
    *,
    content: str = "第1题 学生的合成作答内容。",
):
    submission_id, _ = await _seed_parsed_source(
        client,
        storage,
        engine,
        middle_content=content,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await canonical_service.normalize_submission(factory, storage, submission_id)
    alignment = await alignment_service.align_submission(factory, storage, submission_id)
    bundle = await alignment_service.load_verified_alignment_bundle(
        factory,
        storage,
        submission_id,
    )
    question = bundle.context.questions[0]
    source_region = bundle.alignment_document.answers[0].source_regions[0]
    return factory, submission_id, question, source_region, alignment, bundle


def _payload(decision: str, region, reviewer: str = "local-checker"):
    selection = {
        "content_pointer": region.content_pointer,
        "text_start": region.text_start,
        "text_end": region.text_end,
    }
    fields = {
        "decision": decision,
        "reason_codes": [
            "student_work_visible"
            if decision == "response_present"
            else "printed_prompt_only"
            if decision == "response_absent"
            else "ambiguous_handwriting"
        ],
        "reviewer_ref": reviewer,
    }
    if decision == "response_present":
        fields["response_regions"] = [selection]
    elif decision == "response_absent":
        fields["excluded_prompt_regions"] = [selection]
    else:
        fields["uncertain_regions"] = [selection]
    return AnswerReviewDecisionCreate(**fields)


@pytest.mark.anyio
async def test_review_is_source_bound_append_only_and_does_not_write_s3(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, region, alignment, bundle = await _prepare_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    existing_keys = set(recording_minio_storage.uploaded_keys)

    first, created = await review_service.record_decision(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
        payload=_payload("response_present", region),
    )
    assert created is True
    assert first.decision == "response_present"
    assert first.alignment_artifact_id == alignment.id
    assert first.canonical_document_id == bundle.canonical_artifact.id
    assert first.canonical_sha256 == bundle.canonical_artifact.canonical_sha256
    assert first.revision == 1
    assert first.alignment_status == bundle.alignment_document.answers[0].matching_status
    assert recording_minio_storage.uploaded_keys == existing_keys

    replay, created = await review_service.record_decision(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=first.request_id,
        payload=_payload("response_present", region),
    )
    assert created is False
    assert replay.decision_id == first.decision_id

    second, created = await review_service.record_decision(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
        payload=_payload("uncertain", region),
    )
    assert created is True
    assert second.revision == 2
    assert second.supersedes_decision_id == first.decision_id

    async with factory() as session:
        records = (
            await session.scalars(
                select(AnswerReviewDecision)
                .where(AnswerReviewDecision.submission_id == submission_id)
                .order_by(AnswerReviewDecision.revision)
            )
        ).all()
        current_alignment = await session.scalar(
            select(AnswerAlignmentArtifact).where(AnswerAlignmentArtifact.id == alignment.id)
        )
        current_canonical = await session.scalar(
            select(CanonicalArtifact).where(CanonicalArtifact.id == bundle.canonical_artifact.id)
        )
    assert [item.revision for item in records] == [1, 2]
    assert current_alignment.alignment_sha256 == alignment.alignment_sha256
    assert current_canonical.canonical_sha256 == bundle.canonical_artifact.canonical_sha256

    exported = json.dumps(review_service.export_records([first]), ensure_ascii=False)
    assert "local-checker" not in exported
    assert bundle.context.submission_id.hex not in exported
    assert "object_key" not in exported
    assert "student_ref" not in exported


@pytest.mark.anyio
async def test_response_absent_is_a_reviewed_decision_not_missing_history(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, region, _, _ = await _prepare_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        content="第1题 计算 2+2。",
    )
    decision, created = await review_service.record_decision(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
        payload=_payload("response_absent", region),
    )
    assert created
    assert decision.decision == "response_absent"
    assert decision.response_regions == []
    assert decision.excluded_prompt_regions[0].content_pointer == region.content_pointer

    async with factory() as session:
        history = await review_service.list_decisions(session, submission_id)
    assert [item.decision for item in history] == ["response_absent"]


@pytest.mark.anyio
async def test_uncertain_image_evidence_retains_safe_asset_reference(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    submission_id, _ = await _seed_parsed_source(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        with_image_asset=True,
    )
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    await canonical_service.normalize_submission(factory, recording_minio_storage, submission_id)
    await alignment_service.align_submission(factory, recording_minio_storage, submission_id)
    bundle = await alignment_service.load_verified_alignment_bundle(
        factory,
        recording_minio_storage,
        submission_id,
    )
    question = bundle.context.questions[0]
    source = bundle.alignment_document.unassigned_regions[0].source_regions[0]
    assert source.text_start is not None and source.text_end is not None
    result, created = await review_service.record_decision(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
        payload=AnswerReviewDecisionCreate(
            decision="uncertain",
            uncertain_regions=[
                {
                    "content_pointer": source.content_pointer,
                    "text_start": source.text_start,
                    "text_end": source.text_end,
                }
            ],
            reason_codes=["response_not_linked_to_question"],
            reviewer_ref="local-reviewer",
        ),
    )

    assert created
    references = result.uncertain_regions[0].asset_refs
    assert references
    assert references[0].kind == "stored"
    assert references[0].asset_id is not None
    assert all(not hasattr(reference, "uri") for reference in references)


@pytest.mark.anyio
async def test_invalid_pointer_and_unicode_range_fail_before_insert(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, region, _, bundle = await _prepare_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    invalid = AnswerReviewDecisionCreate.model_validate(
        {
            **_payload("response_present", region).model_dump(),
            "response_regions": [
                {
                    "content_pointer": "/pages/99/blocks/99/content",
                    "text_start": 0,
                    "text_end": 1,
                }
            ],
        }
    )
    with pytest.raises(AnswerReviewRegionInvalidError):
        await review_service.record_decision(
            factory,
            recording_minio_storage,
            submission_id=submission_id,
            question_id=question.id,
            request_id=uuid4(),
            payload=invalid,
        )

    node = bundle.canonical_document.pages[0].blocks[0].content
    too_long = region.model_copy(update={"text_end": len(node.value or "") + 1})
    with pytest.raises(AnswerReviewRegionInvalidError):
        await review_service.record_decision(
            factory,
            recording_minio_storage,
            submission_id=submission_id,
            question_id=question.id,
            request_id=uuid4(),
            payload=_payload("response_present", too_long),
        )

    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AnswerReviewDecision)
            .where(AnswerReviewDecision.submission_id == submission_id)
        )
    assert count == 0


@pytest.mark.anyio
async def test_local_cli_inspect_record_history_and_redacted_export(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    _, submission_id, question, region, _, _ = await _prepare_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    local_settings = Settings()
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": postgres_engine.url.render_as_string(hide_password=False),
            "MINIO_ENDPOINT_URL": local_settings.minio_endpoint_url or "",
            "MINIO_ACCESS_KEY": local_settings.minio_access_key or "",
            "MINIO_SECRET_KEY": local_settings.minio_secret_key or "",
            "MINIO_BUCKET": recording_minio_storage.bucket,
            "MINIO_OBJECT_KEY_PREFIX": recording_minio_storage.object_key_prefix,
        }
    )

    def run_cli(*arguments: str) -> dict[str, object]:
        result = subprocess.run(
            ["uv", "run", "python", "-m", "huipi_cloud.workers.review_answers", *arguments],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    before = run_cli("inspect", "--submission-id", str(submission_id))
    assert before["questions"][0]["answer_presence"] == "unreviewed"
    assert before["questions"][0]["evidence"][0]["page_number"] == 1
    assert before["original_source"]["content_type"] == "application/pdf"
    assert len(before["original_source"]["sha256"]) == 64
    assert "object_key" not in json.dumps(before)

    created = run_cli(
        "record",
        "--submission-id",
        str(submission_id),
        "--question-id",
        str(question.id),
        "--request-id",
        str(uuid4()),
        "--reviewer-ref",
        "local-reviewer",
        "--decision",
        "response_present",
        "--response-region",
        f"{region.content_pointer}:{region.text_start}:{region.text_end}",
        "--reason-code",
        "student_work_visible",
    )
    assert created["status"] == "created"
    assert "grading_readiness" not in created

    current = run_cli("inspect", "--submission-id", str(submission_id))
    assert current["questions"][0]["answer_presence"] == "response_present"
    history = run_cli(
        "history",
        "--submission-id",
        str(submission_id),
        "--question-id",
        str(question.id),
    )
    assert history["revisions"][0]["effective_for_current_source"] is True

    output_path = tmp_path / "redacted-review.json"
    exported = run_cli(
        "export",
        "--submission-id",
        str(submission_id),
        "--output",
        str(output_path),
    )
    assert exported["record_count"] == 1
    export_text = output_path.read_text(encoding="utf-8")
    assert "local-reviewer" not in export_text
    assert str(submission_id) not in export_text
    assert "object_key" not in export_text
    assert "student_ref" not in export_text


@pytest.mark.anyio
async def test_concurrent_revisions_are_serialized_and_request_id_is_idempotent(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, region, _, _ = await _prepare_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    request_id = uuid4()

    async def submit():
        return await review_service.record_decision(
            factory,
            recording_minio_storage,
            submission_id=submission_id,
            question_id=question.id,
            request_id=request_id,
            payload=_payload("response_present", region),
        )

    first, second = await asyncio.gather(submit(), submit())
    assert first[0].decision_id == second[0].decision_id
    assert sorted((first[1], second[1])) == [False, True]

    with pytest.raises(AnswerReviewIdempotencyConflictError):
        await review_service.record_decision(
            factory,
            recording_minio_storage,
            submission_id=submission_id,
            question_id=question.id,
            request_id=request_id,
            payload=_payload("uncertain", region),
        )

    next_results = await asyncio.gather(
        *(
            review_service.record_decision(
                factory,
                recording_minio_storage,
                submission_id=submission_id,
                question_id=question.id,
                request_id=uuid4(),
                payload=_payload("uncertain", region, reviewer=f"reviewer-{index}"),
            )
            for index in range(2)
        )
    )
    assert sorted(item[0].revision for item in next_results) == [2, 3]
    by_revision = {item[0].revision: item[0] for item in next_results}
    assert by_revision[2].supersedes_decision_id == first[0].decision_id
    assert by_revision[3].supersedes_decision_id == by_revision[2].decision_id


@pytest.mark.anyio
async def test_source_change_invalidates_old_decision_for_current_version(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, region, _, old_bundle = await _prepare_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    old, _ = await review_service.record_decision(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
        payload=_payload("response_present", region),
    )
    async with postgres_engine.begin() as connection:
        await connection.execute(
            Question.__table__.update().where(Question.id == question.id).values(question_number=2)
        )
    new_alignment = await alignment_service.align_submission(
        factory,
        recording_minio_storage,
        submission_id,
    )
    new_bundle = await alignment_service.load_verified_alignment_bundle(
        factory,
        recording_minio_storage,
        submission_id,
    )
    assert new_alignment.id != old.alignment_artifact_id
    assert new_bundle.questions_digest != old.assignment_questions_digest
    assert review_service.is_current_for_bundle(old, new_bundle) is False
    assert old_bundle.canonical_artifact.id == new_bundle.canonical_artifact.id


@pytest.mark.anyio
async def test_storage_failure_does_not_create_review_record(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, region, _, _ = await _prepare_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )

    class FailedReadStorage:
        bucket = recording_minio_storage.bucket

        async def download(self, _key):
            raise StorageUnavailableError("test-only private details")

    with pytest.raises(StorageUnavailableError):
        await review_service.record_decision(
            factory,
            FailedReadStorage(),
            submission_id=submission_id,
            question_id=question.id,
            request_id=uuid4(),
            payload=_payload("response_present", region),
        )
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AnswerReviewDecision)
            .where(AnswerReviewDecision.submission_id == submission_id)
        )
    assert count == 0


@pytest.mark.anyio
async def test_database_write_failure_rolls_back_review_decision(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, region, _, _ = await _prepare_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )

    def fail_insert(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().lower().startswith("insert into answer_review_decisions"):
            raise RuntimeError("simulated insert failure")

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="simulated insert failure"):
            await review_service.record_decision(
                factory,
                recording_minio_storage,
                submission_id=submission_id,
                question_id=question.id,
                request_id=uuid4(),
                payload=_payload("response_present", region),
            )
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail_insert)

    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AnswerReviewDecision)
            .where(AnswerReviewDecision.submission_id == submission_id)
        )
    assert count == 0


@pytest.mark.anyio
async def test_unknown_commit_outcome_can_be_replayed_without_duplicate(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, submission_id, question, region, _, _ = await _prepare_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    request_id = uuid4()
    original = review_repository.append_decision
    raised = False

    async def commit_then_report_unknown(*args, **kwargs):
        nonlocal raised
        result = await original(*args, **kwargs)
        if not raised and result[1]:
            raised = True
            raise OSError("simulated lost COMMIT acknowledgment")
        return result

    monkeypatch.setattr(review_repository, "append_decision", commit_then_report_unknown)
    with pytest.raises(OSError, match="COMMIT acknowledgment"):
        await review_service.record_decision(
            factory,
            recording_minio_storage,
            submission_id=submission_id,
            question_id=question.id,
            request_id=request_id,
            payload=_payload("response_present", region),
        )
    monkeypatch.setattr(review_repository, "append_decision", original)

    replay, created = await review_service.record_decision(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=request_id,
        payload=_payload("response_present", region),
    )
    assert created is False
    assert replay.revision == 1
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AnswerReviewDecision)
            .where(AnswerReviewDecision.submission_id == submission_id)
        )
    assert count == 1


@pytest.mark.anyio
async def test_source_binding_is_rechecked_inside_revision_transaction(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, submission_id, question, region, _, _ = await _prepare_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    original = review_repository.append_decision

    async def change_question_then_append(*args, **kwargs):
        async with factory() as session, session.begin():
            current = await session.get(Question, question.id)
            current.question_number = 2
        return await original(*args, **kwargs)

    monkeypatch.setattr(review_repository, "append_decision", change_question_then_append)
    with pytest.raises(AnswerReviewSourceChangedError):
        await review_service.record_decision(
            factory,
            recording_minio_storage,
            submission_id=submission_id,
            question_id=question.id,
            request_id=uuid4(),
            payload=_payload("response_present", region),
        )

    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AnswerReviewDecision)
            .where(AnswerReviewDecision.submission_id == submission_id)
        )
    assert count == 0
