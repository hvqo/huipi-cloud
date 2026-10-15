"""Actual source gates and their current metadata limits, using isolated PG/S3 fixtures."""

from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_canonical_documents import _seed_parsed_source
from test_grade_input import _prepared_review

from huipi_cloud.modules.answer_alignment import service as alignment_service
from huipi_cloud.modules.answer_review import service as review_service
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.answer_review.protocol import AnswerReviewDecisionCreate
from huipi_cloud.modules.canonical_documents import service as canonical_service
from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision
from huipi_cloud.modules.grading.models import GradeInputArtifact, GradeProposalArtifact
from huipi_cloud.modules.grading.service import inspect_grade_input, prepare_grade_input
from huipi_cloud.modules.submissions.models import SubmissionFile


@pytest.mark.anyio
@pytest.mark.parametrize(
    "object_kind", ["canonical_missing", "canonical_modified", "alignment_missing"]
)
async def test_confirmed_source_unavailable_or_changed_never_prepares_zero_score(
    submission_client,
    recording_minio_storage,
    postgres_engine,
    object_kind,
):
    factory, submission_id, question, _ = await _prepared_review(
        submission_client, recording_minio_storage, postgres_engine
    )
    bundle = await alignment_service.load_verified_alignment_bundle(
        factory, recording_minio_storage, submission_id
    )
    key = (
        bundle.alignment_artifact.object_key
        if object_kind == "alignment_missing"
        else bundle.canonical_artifact.canonical_object_key
    )
    if object_kind.endswith("missing"):
        await recording_minio_storage.delete(key)
    else:
        await recording_minio_storage.upload_fileobj(
            BytesIO(b"altered bytes"), key, "application/json"
        )
    uploaded = set(recording_minio_storage.uploaded_keys)
    prepared = await prepare_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
    )
    assert prepared.artifact_id is None
    assert prepared.inspection.status == "source_stale"
    assert prepared.inspection.reason_codes == ["canonical_alignment_invalid"]
    assert set(recording_minio_storage.uploaded_keys) == uploaded
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(GradeInputArtifact)) == 0
        assert await session.scalar(select(func.count()).select_from(AnswerReviewDecision)) == 1


@pytest.mark.anyio
async def test_confirmed_image_with_missing_s3_asset_is_blocked_as_modality(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    submission_id, _ = await _seed_parsed_source(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        with_image_asset=True,
        image_in_text=True,
        middle_content='第1题 <img src="figures/diagram.png">',
    )
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    await canonical_service.normalize_submission(factory, recording_minio_storage, submission_id)
    await alignment_service.align_submission(factory, recording_minio_storage, submission_id)
    bundle = await alignment_service.load_verified_alignment_bundle(
        factory, recording_minio_storage, submission_id
    )
    question = bundle.context.questions[0]
    source = bundle.alignment_document.answers[0].source_regions[0]
    await review_service.record_decision(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
        payload=AnswerReviewDecisionCreate(
            decision="response_present",
            response_regions=[
                {
                    "content_pointer": source.content_pointer,
                    "text_start": source.text_start,
                    "text_end": source.text_end,
                }
            ],
            reason_codes=["student_work_visible"],
            reviewer_ref="synthetic-source-audit",
        ),
    )
    image_key = next(
        k
        for k in recording_minio_storage.uploaded_keys
        if k.endswith("/private-canonical-image-object-key")
    )
    await recording_minio_storage.delete(image_key)
    result = await prepare_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
    )
    assert result.artifact_id is None
    assert result.inspection.status == "unsupported_modality"
    assert bundle.canonical_document.assets
    assert "embedded_image_markup" in result.inspection.reason_codes
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(GradeInputArtifact)) == 0


@pytest.mark.anyio
async def test_question_required_picture_is_not_inferred_from_free_text_stem(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    # A factual boundary: the current Question schema has no trusted required-material field.
    factory, submission_id, question, _ = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        question_stem="依据附图计算阴影面积。",
    )
    # The Question stem cannot substitute for a trusted required-material declaration.
    bundle = await alignment_service.load_verified_alignment_bundle(
        factory, recording_minio_storage, submission_id
    )
    assert bundle.canonical_document.assets == []
    result = await inspect_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
    )
    assert result.status == "ready_text"
    assert not hasattr(question, "required_materials")


@pytest.mark.anyio
async def test_raw_object_change_does_not_rewrite_verified_canonical_snapshot(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    factory, submission_id, question, _ = await _prepared_review(
        submission_client, recording_minio_storage, postgres_engine
    )
    bundle = await alignment_service.load_verified_alignment_bundle(
        factory, recording_minio_storage, submission_id
    )
    original_canonical_sha = bundle.canonical_artifact.canonical_sha256
    async with factory() as session:
        raw = await session.scalar(
            select(SubmissionFile).where(SubmissionFile.submission_id == submission_id)
        )
    await recording_minio_storage.upload_fileobj(
        BytesIO(b"replaced raw bytes"), raw.object_key, raw.content_type
    )
    result = await inspect_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
    )
    assert result.status == "ready_text"
    assert result.canonical_sha256 == original_canonical_sha
    # GradeInput verifies the Canonical snapshot, not the live raw S3 bytes on every inspection.
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(GradeInputArtifact)) == 0


@pytest.mark.anyio
async def test_experimental_prompt_requests_do_not_write_proposals_or_grade_reviews(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    from huipi_cloud.core.config import Settings
    from huipi_cloud.modules.grading.evaluation import load_dataset
    from huipi_cloud.modules.grading.quote_evaluation import request_experiment_prediction
    from huipi_cloud.modules.grading.quote_semantics import SEMANTICS_PROMPT_VERSION
    from tests.unit.test_rubric_semantics import FakeProvider, reference_reply

    factory, _, _, _ = await _prepared_review(
        submission_client, recording_minio_storage, postgres_engine
    )
    async with factory() as session:
        before = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (GradeProposalArtifact, GradeReviewRevision, AnswerReviewDecision)
        ]
    root = Path(__file__).parents[2]
    ds, _ = load_dataset(root / "data/evaluation/grading/rubric-cases-v4-semantics.json")
    case = ds.cases[0]
    provider = FakeProvider(reference_reply(case))
    prediction = await request_experiment_prediction(
        case,
        "quote_selection",
        provider,
        Settings(_env_file=None),
        prompt_version=SEMANTICS_PROMPT_VERSION,
    )
    assert prediction.status == "candidate" and len(provider.calls) == 1
    async with factory() as session:
        after = [
            await session.scalar(select(func.count()).select_from(model))
            for model in (GradeProposalArtifact, GradeReviewRevision, AnswerReviewDecision)
        ]
    assert before == after


@pytest.mark.anyio
async def test_confirmed_table_selection_never_prepares_text_input(
    submission_client,
    recording_minio_storage,
    postgres_engine,
):
    text = "学生作答：<table><tr><td>4</td></tr></table>"
    factory, submission_id, question, _ = await _prepared_review(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        middle_content="第1题 计算题干：2+2。" + text,
        response_text=text,
    )
    result = await prepare_grade_input(
        factory,
        recording_minio_storage,
        submission_id=submission_id,
        question_id=question.id,
        request_id=uuid4(),
    )
    assert result.artifact_id is None
    assert result.inspection.status == "unsupported_modality"
    assert "embedded_table_markup" in result.inspection.reason_codes
