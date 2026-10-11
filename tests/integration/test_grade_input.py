"""Real PostgreSQL/MinIO coverage for trusted GradeInput preparation."""

from __future__ import annotations

import asyncio
import hashlib
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from huipi_cloud.infrastructure.storage.s3 import (
    StorageObjectNotFoundError,
    StorageUnavailableError,
)
from huipi_cloud.modules.answer_alignment import service as alignment_service
from huipi_cloud.modules.answer_review import service as review_service
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.answer_review.protocol import (
    AnswerReviewDecisionCreate,
    ReviewRegionSelection,
)
from huipi_cloud.modules.assignments.models import AnswerKey, Assignment, RubricCriterion
from huipi_cloud.modules.canonical_documents import service as canonical_service
from huipi_cloud.modules.grading import repository as grade_repository
from huipi_cloud.modules.grading.errors import (
    GradeInputCommitOutcomeUnknownError,
    GradeInputRegistrationRolledBackError,
    GradeInputSourceChangedError,
)
from huipi_cloud.modules.grading.models import GradeInputArtifact
from huipi_cloud.modules.grading.protocol import GradeInputDocument
from huipi_cloud.modules.grading.service import (
    inspect_grade_input,
    prepare_grade_input,
    read_grade_input_artifact,
)
from huipi_cloud.modules.visual_evidence.models import VisualEvidenceArtifact

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "integration") not in sys.path:
    sys.path.insert(0, str(ROOT / "integration"))
from test_canonical_documents import _seed_parsed_source  # noqa: E402

_MIDDLE_CONTENT = "第1题 计算题干：2+2。学生作答：4。"
_PROMPT = "计算题干：2+2。"
_RESPONSE = "学生作答：4。"


async def _prepared_review(
    client: httpx.AsyncClient,
    storage,
    engine: AsyncEngine,
    *,
    decision: str = "response_present",
    middle_content: str = _MIDDLE_CONTENT,
    response_text: str = _RESPONSE,
):
    submission_id, _ = await _seed_parsed_source(
        client,
        storage,
        engine,
        middle_content=middle_content,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await canonical_service.normalize_submission(factory, storage, submission_id)
    await alignment_service.align_submission(factory, storage, submission_id)
    bundle = await alignment_service.load_verified_alignment_bundle(
        factory,
        storage,
        submission_id,
    )
    question = bundle.context.questions[0]
    pointer = "/pages/0/blocks/0/content"
    prompt_start = middle_content.index(_PROMPT)
    response_start = middle_content.index(response_text)
    prompt = ReviewRegionSelection(
        content_pointer=pointer,
        text_start=prompt_start,
        text_end=prompt_start + len(_PROMPT),
    )
    response = ReviewRegionSelection(
        content_pointer=pointer,
        text_start=response_start,
        text_end=response_start + len(response_text),
    )
    if decision == "response_present":
        payload = AnswerReviewDecisionCreate(
            decision=decision,
            response_regions=[response],
            excluded_prompt_regions=[prompt],
            reason_codes=["student_work_visible", "printed_prompt_only"],
            reviewer_ref="local-grade-input-test",
        )
    elif decision == "response_absent":
        payload = AnswerReviewDecisionCreate(
            decision=decision,
            excluded_prompt_regions=[prompt],
            reason_codes=["printed_prompt_only"],
            reviewer_ref="local-grade-input-test",
        )
    else:
        payload = AnswerReviewDecisionCreate(
            decision="uncertain",
            uncertain_regions=[response],
            reason_codes=["ambiguous_handwriting"],
            reviewer_ref="local-grade-input-test",
        )
    review, _ = await review_service.record_decision(
        factory,
        storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
        payload=payload,
    )
    return factory, submission_id, question, review


@pytest.mark.anyio
async def test_synthetic_submission_review_to_grade_input_round_trip(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, review = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    request_id = uuid4()

    inspection = await inspect_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=request_id,
    )
    assert inspection.status == "ready_text"
    assert inspection.review_decision_id == review.decision_id
    assert inspection.review_revision == 1
    assert inspection.rubric_total_points == Decimal("10.00")
    assert inspection.segment_count == 1
    assert inspection.selected_text_characters == len(_RESPONSE)
    assert _RESPONSE not in inspection.model_dump_json()
    assert _PROMPT not in inspection.model_dump_json()

    prepared = await prepare_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=request_id,
    )
    assert prepared.artifact_id is not None
    assert prepared.created is True
    assert prepared.input_sha256 == inspection.input_sha256

    document = await read_grade_input_artifact(
        factory,
        recording_minio_storage,
        prepared.artifact_id,
    )
    assert isinstance(document, GradeInputDocument)
    assert document.selected_answer_segments[0].text == _RESPONSE
    assert _PROMPT not in "".join(item.text for item in document.selected_answer_segments)
    assert document.question.question_id == question.id
    assert document.question.max_score == Decimal("10.00")
    assert document.answer_key.answer_content == "集成测试答案"
    assert document.answer_key.source == "teacher"
    assert sum((item.points for item in document.rubric_snapshot), Decimal("0")) == Decimal(
        "10.00"
    )
    assert document.review_decision_id == review.decision_id
    assert document.excluded_prompt_regions_sha256
    assert all("object_key" not in item.model_dump_json() for item in document.evidence_manifest)

    async with factory() as session:
        indexed = await grade_repository.get_artifact(session, prepared.artifact_id)
        review_count = await session.scalar(
            select(func.count())
            .select_from(AnswerReviewDecision)
            .where(AnswerReviewDecision.submission_id == submission_id)
        )
        visual_count = await session.scalar(
            select(func.count())
            .select_from(VisualEvidenceArtifact)
            .where(VisualEvidenceArtifact.submission_id == submission_id)
        )
    assert indexed is not None
    assert indexed.input_sha256 == prepared.input_sha256
    assert indexed.object_sha256 == prepared.object_sha256
    assert review_count == 1
    assert visual_count == 0

    downloaded = await recording_minio_storage.download(indexed.object_key)
    object_body = b"".join([chunk async for chunk in downloaded.chunks()])
    assert len(object_body) == indexed.object_size_bytes
    assert hashlib.sha256(object_body).hexdigest() == indexed.object_sha256
    stored = GradeInputDocument.model_validate_json(object_body)
    assert stored.input_sha256 == document.input_sha256
    assert stored.selected_answer_segments == document.selected_answer_segments
    assert "object_key" not in object_body.decode("utf-8")

    uploaded_before_replay = set(recording_minio_storage.uploaded_keys)
    replay = await prepare_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=request_id,
    )
    assert replay.created is False
    assert replay.artifact_id == prepared.artifact_id
    assert set(recording_minio_storage.uploaded_keys) == uploaded_before_replay


@pytest.mark.anyio
@pytest.mark.parametrize("decision", ["uncertain", "response_absent"])
async def test_non_present_human_decision_never_creates_a_grade_input(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    decision: str,
) -> None:
    factory, submission_id, question, _ = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        decision=decision,
    )
    previous_uploads = set(recording_minio_storage.uploaded_keys)

    result = await prepare_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
    )

    assert result.artifact_id is None
    assert result.inspection.status == "review_required"
    assert f"human_review_{decision}" in result.inspection.reason_codes
    assert set(recording_minio_storage.uploaded_keys) == previous_uploads
    async with factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(GradeInputArtifact)
        )
    assert count == 0


@pytest.mark.anyio
async def test_unreviewed_input_is_not_prepared(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    submission_id, _ = await _seed_parsed_source(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        middle_content=_MIDDLE_CONTENT,
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

    inspected = await inspect_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
    )
    assert inspected.status == "review_required"
    assert "human_review_missing" in inspected.reason_codes


@pytest.mark.anyio
async def test_concurrent_prepare_registers_one_index_and_keeps_one_s3_object(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, _ = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    request_id = uuid4()
    recording_minio_storage.after_upload_barrier = asyncio.Barrier(2)
    first, second = await asyncio.gather(
        prepare_grade_input(
            factory,
            recording_minio_storage,
            submission_id=submission_id,
            question_id=question.id,
            request_id=request_id,
        ),
        prepare_grade_input(
            factory,
            recording_minio_storage,
            submission_id=submission_id,
            question_id=question.id,
            request_id=request_id,
        ),
    )
    recording_minio_storage.after_upload_barrier = None

    assert first.artifact_id == second.artifact_id
    assert {first.created, second.created} == {True, False}
    attempts = [
        key for key in recording_minio_storage.uploaded_keys if "/grading/inputs/" in key
    ]
    assert len(attempts) == 2
    async with factory() as session:
        rows = (
            await session.scalars(
                select(GradeInputArtifact).where(
                    GradeInputArtifact.submission_id == submission_id,
                    GradeInputArtifact.question_id == question.id,
                    GradeInputArtifact.request_id == request_id,
                )
            )
        ).all()
    assert len(rows) == 1
    live_objects = []
    for key in attempts:
        try:
            await recording_minio_storage.head_object(key)
        except Exception:
            continue
        live_objects.append(key)
    assert live_objects == [rows[0].object_key]


@pytest.mark.anyio
async def test_postgres_failure_before_commit_rolls_back_and_cleans_uploaded_object(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, _ = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )

    def fail_grade_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if "INSERT INTO grade_input_artifacts" in statement:
            raise OperationalError("insert", {}, RuntimeError("simulated PG failure"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail_grade_insert)
    try:
        with pytest.raises(GradeInputRegistrationRolledBackError):
            await prepare_grade_input(
                factory,
                recording_minio_storage,
                submission_id=submission_id,
                question_id=question.id,
                request_id=uuid4(),
            )
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail_grade_insert)

    attempts = [
        key for key in recording_minio_storage.uploaded_keys if "/grading/inputs/" in key
    ]
    assert len(attempts) == 1
    with pytest.raises(StorageObjectNotFoundError):
        await recording_minio_storage.head_object(attempts[0])
    async with factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(GradeInputArtifact)
        )
    assert count == 0


@pytest.mark.anyio
async def test_storage_upload_failure_creates_no_index_and_cleans_completed_attempt(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, submission_id, question, _ = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    real_upload = recording_minio_storage.upload_fileobj
    attempts: list[str] = []

    async def upload_then_report_failure(fileobj, object_key, content_type):
        await real_upload(fileobj, object_key, content_type)
        if "/grading/inputs/" in object_key:
            attempts.append(object_key)
            raise StorageUnavailableError("redacted storage failure")

    monkeypatch.setattr(recording_minio_storage, "upload_fileobj", upload_then_report_failure)
    with pytest.raises(StorageUnavailableError):
        await prepare_grade_input(
            factory,
            recording_minio_storage,
            submission_id=submission_id,
            question_id=question.id,
            request_id=uuid4(),
        )

    assert len(attempts) == 1
    with pytest.raises(StorageObjectNotFoundError):
        await recording_minio_storage.head_object(attempts[0])
    async with factory() as session:
        count = await session.scalar(select(func.count()).select_from(GradeInputArtifact))
    assert count == 0


@pytest.mark.anyio
async def test_unknown_commit_keeps_indexed_object_and_same_request_can_replay(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, submission_id, question, _ = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    request_id = uuid4()
    real_register = grade_repository.register_success

    async def commit_then_lose_confirmation(*args, **kwargs):
        await real_register(*args, **kwargs)
        raise GradeInputCommitOutcomeUnknownError

    monkeypatch.setattr(grade_repository, "register_success", commit_then_lose_confirmation)
    with pytest.raises(GradeInputCommitOutcomeUnknownError):
        await prepare_grade_input(
            factory,
            recording_minio_storage,
            submission_id=submission_id,
            question_id=question.id,
            request_id=request_id,
        )
    monkeypatch.setattr(grade_repository, "register_success", real_register)

    async with factory() as session:
        record = await session.scalar(
            select(GradeInputArtifact).where(
                GradeInputArtifact.submission_id == submission_id,
                GradeInputArtifact.question_id == question.id,
                GradeInputArtifact.request_id == request_id,
            )
        )
    assert record is not None
    await recording_minio_storage.head_object(record.object_key)
    replay = await prepare_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=request_id,
    )
    assert replay.artifact_id == record.id
    assert replay.created is False


@pytest.mark.anyio
async def test_latest_review_revision_invalidates_old_package_for_current_consumption(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, first_review = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    prepared = await prepare_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
    )
    assert prepared.artifact_id is not None
    bundle = await alignment_service.load_verified_alignment_bundle(
        factory,
        recording_minio_storage,
        submission_id,
    )
    node_text = bundle.canonical_document.pages[0].blocks[0].content.value
    assert node_text is not None
    start = node_text.index(_RESPONSE)
    region = ReviewRegionSelection(
        content_pointer="/pages/0/blocks/0/content",
        text_start=start,
        text_end=start + len(_RESPONSE),
    )
    later, _ = await review_service.record_decision(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
        payload=AnswerReviewDecisionCreate(
            decision="uncertain",
            uncertain_regions=[region],
            reason_codes=["ambiguous_handwriting"],
            reviewer_ref="local-grade-input-test",
        ),
    )
    assert later.revision == first_review.revision + 1

    with pytest.raises(GradeInputSourceChangedError, match="grade_input_no_longer_current"):
        await read_grade_input_artifact(
            factory,
            recording_minio_storage,
            prepared.artifact_id,
        )


@pytest.mark.anyio
async def test_invalid_rubric_total_blocks_input_even_if_human_review_is_present(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, _ = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    async with postgres_engine.begin() as connection:
        await connection.execute(
            update(RubricCriterion)
            .where(RubricCriterion.question_id == question.id)
            .values(points=Decimal("9.99"))
        )

    result = await inspect_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
    )
    assert result.status == "invalid_rubric"
    assert "rubric_total_or_fields_invalid" in result.reason_codes


@pytest.mark.anyio
async def test_confirmed_text_with_embedded_image_blocks_inspect_and_prepare_without_persistence(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    embedded_image = '学生作答：4。<img src="figures/answer.png">'
    middle_content = f"第1题 {_PROMPT}{embedded_image}"
    factory, submission_id, question, _ = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        middle_content=middle_content,
        response_text=embedded_image,
    )
    request_id = uuid4()
    uploads_before = set(recording_minio_storage.uploaded_keys)

    inspection = await inspect_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=request_id,
    )
    prepared = await prepare_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=request_id,
    )

    assert inspection.status == "unsupported_modality"
    assert "embedded_image_markup" in inspection.reason_codes
    assert "figures/answer.png" not in inspection.model_dump_json()
    assert inspection.segment_count == 0
    assert len(inspection.evidence_manifest) == 1
    assert inspection.evidence_manifest[0].representation == "unsupported"
    assert inspection.evidence_manifest[0].text_start is not None
    assert prepared.artifact_id is None
    assert prepared.inspection.status == "unsupported_modality"
    assert set(recording_minio_storage.uploaded_keys) == uploads_before
    async with factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(GradeInputArtifact)
            .where(GradeInputArtifact.submission_id == submission_id)
        )
    assert count == 0


@pytest.mark.anyio
async def test_missing_answer_key_and_unpublished_assignment_are_blocked(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    factory, submission_id, question, _ = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    async with postgres_engine.begin() as connection:
        await connection.execute(
            update(Assignment)
            .where(Assignment.id == question.assignment_id)
            .values(status="draft")
        )
        await connection.execute(delete(AnswerKey).where(AnswerKey.question_id == question.id))

    result = await inspect_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
    )
    assert result.status == "invalid_rubric"
    assert "assignment_not_published" in result.reason_codes
    assert "answer_key_missing" in result.reason_codes
