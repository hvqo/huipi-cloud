"""Homework adapter for the shared image scoring and human-review protocol.

Every operation resolves the current teacher-authorized attempt. Images come only
from its immutable private submission file; clients never supply object keys.
"""

import asyncio
import hashlib
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import async_sessionmaker

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider
from huipi_cloud.infrastructure.storage.s3 import (
    StorageObjectNotFoundError,
    StorageUnavailableError,
)
from huipi_cloud.infrastructure.visual_evidence.render import render_selected_pages
from huipi_cloud.modules.answer_alignment.errors import (
    AnswerAlignmentArtifactCorruptError,
    AnswerAlignmentLimitError,
    AnswerAlignmentNotFoundError,
    AnswerAlignmentNotReadyError,
    AnswerAlignmentResultNotFoundError,
)
from huipi_cloud.modules.answer_alignment.matcher import assignment_questions_digest
from huipi_cloud.modules.answer_alignment.service import load_verified_alignment_bundle
from huipi_cloud.modules.assignments.models import AnswerKey, Question, RubricCriterion
from huipi_cloud.modules.canonical_documents.errors import CanonicalNormalizationError
from huipi_cloud.modules.canonical_documents.service import _read_checked_object
from huipi_cloud.modules.exam_grading import visual_scoring, visual_selection
from huipi_cloud.modules.exam_grading.visual_protocol import (
    VisualProposal,
    VisualReview,
    VisualReviewCriterion,
    VisualReviewDecision,
)
from huipi_cloud.modules.grading import grade_review_service as review_service
from huipi_cloud.modules.grading.errors import GradeInputCommitOutcomeUnknownError, GradeInputError
from huipi_cloud.modules.grading.grade_review_errors import GradeReviewError
from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision
from huipi_cloud.modules.grading.grade_review_protocol import grade_review_request_sha256
from huipi_cloud.modules.grading.models import GradeInputArtifact, GradeProposalArtifact
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError
from huipi_cloud.modules.grading.proposal_service import _read_current_input
from huipi_cloud.modules.grading.protocol import (
    AnswerKeySnapshot,
    GradeInputQuestionSnapshot,
    RubricCriterionSnapshot,
    json_sha256,
    rubric_sha256,
)
from huipi_cloud.modules.grading.service import prepare_grade_input
from huipi_cloud.modules.identity.models import User
from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.scans.pdf import previews
from huipi_cloud.modules.submissions.models import Submission, SubmissionFile

from . import access
from .visual_models import HomeworkVisualJob, HomeworkVisualSelection
from .visual_protocol import HomeworkVisualInput, HomeworkVisualInputCore, HomeworkVisualRegion

V, J = HomeworkVisualSelection.__table__, HomeworkVisualJob.__table__
GI, GP, GR = (
    GradeInputArtifact.__table__,
    GradeProposalArtifact.__table__,
    GradeReviewRevision.__table__,
)
F = SubmissionFile.__table__
PROMPT_VERSION = "homework-visual-rubric-v1"


async def context(db, principal, submission_id, question_id, *, write=False, lock=False):
    attempt = await access.submission_access(db, principal, submission_id, write=write, lock=lock)
    if not attempt or not attempt["is_current"]:
        raise AccessError("homework_attempt_superseded", 409)
    row = (
        (
            await db.execute(
                select(F, Submission.assignment_id)
                .join(Submission, Submission.id == F.c.submission_id)
                .where(F.c.submission_id == submission_id)
            )
        )
        .mappings()
        .first()
    )
    if not row or row["sha256"] != attempt["file_sha256"]:
        raise AccessError("homework_visual_source_changed", 409)
    if question_id is not None and not await db.scalar(
        select(Question.id).where(
            Question.id == question_id, Question.assignment_id == row["assignment_id"]
        )
    ):
        raise AccessError(access.DENIED, 404)
    return attempt, row


async def actor(db, user_id):
    user = await db.get(User, user_id, populate_existing=True)
    if not user or not user.is_active:
        raise AccessError("homework_visual_authority_revoked", 404)
    return Principal(
        user_id=user.id,
        issuer=user.issuer,
        subject=user.subject,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


async def source_bytes(storage, file):
    if file["bucket"] != storage.bucket:
        raise AccessError("homework_visual_source_unavailable", 503)
    try:
        data = await _read_checked_object(
            storage,
            file["object_key"],
            expected_size=file["size_bytes"],
            expected_sha256=file["sha256"],
            maximum=settings.max_upload_size_bytes,
        )
        return bytes(data.data)
    except Exception as error:
        if isinstance(error, AccessError):
            raise
        raise AccessError("homework_visual_source_unavailable", 503) from None


async def render_pages(data, content_type, indexes):
    if len(set(indexes)) > 8:
        raise AccessError("homework_visual_region_limit", 413)
    if content_type == "application/pdf":
        with tempfile.TemporaryDirectory(prefix="huipi-homework-visual-") as directory:
            root = Path(directory)
            await asyncio.to_thread((root / "original.pdf").write_bytes, data)
            pages = await previews(root, min(settings.mineru_max_pdf_pages, 50), 60)
            if any(i >= len(pages) for i in indexes):
                raise AccessError("homework_visual_page_invalid", 422)
            return {
                i: await asyncio.to_thread((root / f"page-{i}.jpg").read_bytes)
                for i in set(indexes)
            }
    try:
        values = await asyncio.to_thread(
            render_selected_pages,
            data,
            content_type=content_type,
            selected_page_indexes=sorted(set(indexes)),
            config=settings.model_copy(
                update={"visual_evidence_max_page_image_bytes": 2 * 1024 * 1024}
            ),
        )
        return {p.page_index: p.image_bytes for p in values}
    except Exception:
        raise AccessError("homework_visual_page_invalid", 422) from None


async def preview(db, principal, storage, submission_id, page_index):
    async with db.begin():
        attempt, file = await context(db, principal, submission_id, None)
        attempt_id = attempt["id"]
    data = await source_bytes(storage, file)
    images = await render_pages(data, file["content_type"], [page_index])
    async with db.begin():
        current, _ = await context(db, principal, submission_id, None, lock=True)
        if current["id"] != attempt_id:
            raise AccessError("homework_visual_source_changed", 409)
    return images[page_index]


async def latest_selection(db, attempt_id, question_id):
    return (
        (
            await db.execute(
                select(V)
                .where(V.c.attempt_id == attempt_id, V.c.question_id == question_id)
                .order_by(V.c.revision.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )


def verified_regions(selection):
    regions = [HomeworkVisualRegion.model_validate(r) for r in selection["regions"]]
    if [r.region_index for r in regions] != list(range(len(regions))) or json_sha256(
        regions
    ) != selection["regions_sha256"]:
        raise AccessError("homework_visual_selection_changed", 409)
    return regions


async def materialize(storage, file, regions):
    if any(
        r.submission_file_id != file["id"] or r.source_file_sha256 != file["sha256"]
        for r in regions
    ):
        raise AccessError("homework_visual_source_changed", 409)
    images = await render_pages(
        await source_bytes(storage, file),
        file["content_type"],
        [r.source_page_index for r in regions],
    )
    result = []
    for region in regions:
        page = images[region.source_page_index]
        if hashlib.sha256(page).hexdigest() != region.page_sha256:
            raise AccessError("homework_visual_source_changed", 409)
        cropped, _, _ = await asyncio.to_thread(
            visual_selection.crop,
            page,
            (region.x0, region.y0, region.x1, region.y1),
            (region.width, region.height),
        )
        if hashlib.sha256(cropped).hexdigest() != region.crop_sha256:
            raise AccessError("homework_visual_source_changed", 409)
        result.append(cropped)
    return result


async def select_regions(db, principal, storage, submission_id, question_id, request):
    digest = json_sha256(
        dict(
            submission_id=str(submission_id),
            question_id=str(question_id),
            request=request.model_dump(mode="json"),
        )
    )
    async with db.begin():
        attempt, file = await context(
            db, principal, submission_id, question_id, write=True, lock=True
        )
        old = (
            (
                await db.execute(
                    select(V).where(
                        V.c.actor_user_id == principal.user_id, V.c.request_id == request.request_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if old:
            if old["request_sha256"] != digest:
                raise AccessError("homework_visual_request_conflict", 409)
            return {"selection_id": str(old["id"]), "revision": old["revision"], "created": False}
        current = await latest_selection(db, attempt["id"], question_id)
        if (current["revision"] if current else 0) != request.expected_revision:
            raise AccessError("homework_visual_revision_conflict", 409)
        attempt_id = attempt["id"]
    pages = await render_pages(
        await source_bytes(storage, file),
        file["content_type"],
        [r.page_index for r in request.regions],
    )
    values = []
    for ordinal, rect in enumerate(request.regions):
        from io import BytesIO

        from PIL import Image

        page = pages[rect.page_index]
        with Image.open(BytesIO(page)) as image:
            width, height = image.size
        coords = (
            rect.x0 * width // 10000,
            rect.y0 * height // 10000,
            (rect.x1 * width + 9999) // 10000,
            (rect.y1 * height + 9999) // 10000,
        )
        cropped, _, _ = await asyncio.to_thread(visual_selection.crop, page, coords)
        values.append(
            HomeworkVisualRegion(
                region_index=ordinal,
                submission_file_id=file["id"],
                source_page_index=rect.page_index,
                source_file_sha256=file["sha256"],
                page_sha256=hashlib.sha256(page).hexdigest(),
                crop_sha256=hashlib.sha256(cropped).hexdigest(),
                width=width,
                height=height,
                x0=coords[0],
                y0=coords[1],
                x1=coords[2],
                y1=coords[3],
            )
        )
    async with db.begin():
        attempt, current_file = await context(
            db, principal, submission_id, question_id, write=True, lock=True
        )
        old = (
            (
                await db.execute(
                    select(V).where(
                        V.c.actor_user_id == principal.user_id, V.c.request_id == request.request_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if old:
            if old["request_sha256"] != digest:
                raise AccessError("homework_visual_request_conflict", 409)
            return {"selection_id": str(old["id"]), "revision": old["revision"], "created": False}
        current = await latest_selection(db, attempt["id"], question_id)
        if attempt["id"] != attempt_id or current_file["sha256"] != file["sha256"]:
            raise AccessError("homework_visual_source_changed", 409)
        if (current["revision"] if current else 0) != request.expected_revision:
            raise AccessError("homework_visual_revision_conflict", 409)
        other_selections = (
            (
                await db.execute(
                    select(V)
                    .where(
                        V.c.attempt_id == attempt_id,
                        V.c.question_id != question_id,
                    )
                    .ext(distinct_on(V.c.question_id))
                    .order_by(V.c.question_id, V.c.revision.desc())
                )
            )
            .mappings()
            .all()
        )
        for other in other_selections:
            for existing in verified_regions(other):
                for item in values:
                    if (
                        item.source_page_index == existing.source_page_index
                        and max(item.x0, existing.x0) < min(item.x1, existing.x1)
                        and max(item.y0, existing.y0) < min(item.y1, existing.y1)
                    ):
                        raise AccessError("homework_visual_region_owned_by_other_question", 409)
        identifier = uuid4()
        await db.execute(
            insert(V).values(
                id=identifier,
                attempt_id=attempt_id,
                submission_id=submission_id,
                file_id=file["id"],
                question_id=question_id,
                revision=request.expected_revision + 1,
                regions=[v.model_dump(mode="json") for v in values],
                regions_sha256=json_sha256(values),
                actor_user_id=principal.user_id,
                request_id=request.request_id,
                request_sha256=digest,
            )
        )
        return {
            "selection_id": str(identifier),
            "revision": request.expected_revision + 1,
            "created": True,
        }


async def store_document(storage, kind, identifier, doc, maximum):
    from io import BytesIO

    from huipi_cloud.modules.grading.protocol import canonical_json_bytes

    payload = canonical_json_bytes(doc)
    if len(payload) > maximum:
        raise AccessError("homework_visual_document_too_large", 413)
    digest = hashlib.sha256(payload).hexdigest()
    key = f"{storage.object_key_prefix}/homework-visual/{kind}/{identifier}/{uuid4()}.json"
    try:
        await storage.upload_fileobj(BytesIO(payload), key, "application/json")
        await _read_checked_object(
            storage, key, expected_size=len(payload), expected_sha256=digest, maximum=maximum
        )
    except (StorageUnavailableError, StorageObjectNotFoundError, CanonicalNormalizationError):
        raise AccessError("homework_visual_source_unavailable", 503) from None
    # A DB COMMIT acknowledgement may be lost: never delete a potentially referenced object.
    return dict(
        bucket=storage.bucket, object_key=key, object_sha256=digest, object_size_bytes=len(payload)
    )


async def read_document(storage, row, model, maximum):
    if row["bucket"] != storage.bucket:
        raise AccessError("homework_visual_source_unavailable", 503)
    try:
        body = await _read_checked_object(
            storage,
            row["object_key"],
            expected_size=row["object_size_bytes"],
            expected_sha256=row["object_sha256"],
            maximum=maximum,
        )
    except (StorageUnavailableError, StorageObjectNotFoundError, CanonicalNormalizationError):
        raise AccessError("homework_visual_source_unavailable", 503) from None
    try:
        return model.model_validate_json(body.data)
    except Exception:
        raise AccessError("homework_visual_document_invalid", 409) from None


async def checked_selection(
    db, principal, submission_id, question_id, selection_id=None, *, write=False, lock=False
):
    attempt, file = await context(db, principal, submission_id, question_id, write=write, lock=lock)
    selection = await latest_selection(db, attempt["id"], question_id)
    if not selection or (selection_id and selection["id"] != selection_id):
        raise AccessError("homework_visual_selection_changed", 409)
    # The selection is a teacher decision; its creator must still have current authority.
    author = await actor(db, selection["actor_user_id"])
    await context(db, author, submission_id, question_id, write=True, lock=lock)
    regions = verified_regions(selection)
    if any(
        r.submission_file_id != file["id"] or r.source_file_sha256 != file["sha256"]
        for r in regions
    ):
        raise AccessError("homework_visual_source_changed", 409)
    return attempt, file, selection, regions


async def confirmed_bundle(factory, storage, submission_id):
    try:
        return await load_verified_alignment_bundle(factory, storage, submission_id)
    except (
        AnswerAlignmentArtifactCorruptError,
        AnswerAlignmentLimitError,
        AnswerAlignmentNotFoundError,
        AnswerAlignmentNotReadyError,
        AnswerAlignmentResultNotFoundError,
    ):
        raise AccessError("homework_visual_confirmed_text_unavailable", 409) from None


async def build_core(
    db, factory, storage, principal, submission_id, question_id, selection, regions, file, request
):
    bundle = None
    if request.include_confirmed_text:
        bundle = await confirmed_bundle(factory, storage, submission_id)
    async with db.begin():
        await checked_selection(
            db, principal, submission_id, question_id, selection["id"], write=True, lock=True
        )
        question = await db.scalar(select(Question).where(Question.id == question_id))
        answer = await db.scalar(select(AnswerKey).where(AnswerKey.question_id == question_id))
        criteria = (
            await db.scalars(
                select(RubricCriterion)
                .where(RubricCriterion.question_id == question_id)
                .order_by(RubricCriterion.sort_order)
            )
        ).all()
        questions = (
            await db.scalars(
                select(Question)
                .where(Question.assignment_id == file["assignment_id"])
                .order_by(Question.question_number, Question.id)
            )
        ).all()
        if not question or not answer or answer.source != "teacher" or not criteria:
            raise AccessError("homework_rubric_invalid", 409)
        question_snapshot = GradeInputQuestionSnapshot(
            question_id=question.id,
            question_number=question.question_number,
            question_type=question.question_type,
            stem=question.stem,
            max_score=question.max_score,
        )
        answer_snapshot = AnswerKeySnapshot(
            answer_key_id=answer.id,
            question_id=question.id,
            source=answer.source,
            answer_content=answer.answer_content,
            sha256=hashlib.sha256(answer.answer_content.encode()).hexdigest(),
        )
        rubric = [
            RubricCriterionSnapshot(
                criterion_id=r.id,
                question_id=r.question_id,
                description=r.description,
                points=r.points,
                sort_order=r.sort_order,
            )
            for r in criteria
        ]
    text = []
    text_id = text_sha = None
    if request.include_confirmed_text:
        try:
            prepared = await prepare_grade_input(
                factory,
                storage,
                submission_id=submission_id,
                question_id=question_id,
                request_id=uuid5(NAMESPACE_URL, f"homework-visual-text:{request.request_id}"),
            )
        except GradeInputCommitOutcomeUnknownError:
            raise AccessError("homework_visual_input_commit_unknown", 503) from None
        except GradeInputError:
            raise AccessError("homework_visual_confirmed_text_unavailable", 409) from None
        if not prepared.artifact_id:
            raise AccessError("homework_visual_confirmed_text_unavailable", 409)
        trusted = await _read_current_input(factory, storage, prepared.artifact_id, config=settings)
        text, text_id, text_sha = (
            trusted.selected_answer_segments,
            trusted.artifact_id,
            trusted.input_sha256,
        )
    try:
        return HomeworkVisualInputCore(
            request_id=request.request_id,
            selection_id=selection["id"],
            selection_revision=selection["revision"],
            selection_sha256=selection["regions_sha256"],
            submission_id=submission_id,
            assignment_id=file["assignment_id"],
            question=question_snapshot,
            canonical_artifact_id=bundle.canonical_artifact.id if bundle else None,
            canonical_sha256=bundle.canonical_artifact.canonical_sha256 if bundle else None,
            alignment_artifact_id=bundle.alignment_artifact.id if bundle else None,
            alignment_sha256=bundle.alignment_artifact.alignment_sha256 if bundle else None,
            assignment_questions_digest=assignment_questions_digest(questions),
            source_file_sha256=file["sha256"],
            answer_key=answer_snapshot,
            rubric_snapshot=rubric,
            rubric_sha256=rubric_sha256(rubric),
            regions=regions,
            confirmed_text=text,
            confirmed_text_input_id=text_id,
            confirmed_text_input_sha256=text_sha,
        )
    except ValueError:
        raise AccessError("homework_rubric_invalid", 409) from None


async def verify_text(factory, storage, doc):
    if doc.confirmed_text_input_id:
        try:
            current = await _read_current_input(
                factory, storage, doc.confirmed_text_input_id, config=settings
            )
        except (GradeInputError, GradeProposalError):
            raise AccessError("homework_visual_confirmed_text_changed", 409) from None
        if (
            current.input_sha256 != doc.confirmed_text_input_sha256
            or current.selected_answer_segments != doc.confirmed_text
        ):
            raise AccessError("homework_visual_confirmed_text_changed", 409)


async def verify_document(db, factory, storage, principal, submission_id, question_id, doc):
    """Revalidate the original sources and teacher authority before serving or saving a result."""
    if doc.confirmed_text:
        bundle = await confirmed_bundle(factory, storage, submission_id)
        if (
            bundle.canonical_artifact.id != doc.canonical_artifact_id
            or bundle.canonical_artifact.canonical_sha256 != doc.canonical_sha256
            or bundle.alignment_artifact.id != doc.alignment_artifact_id
            or bundle.alignment_artifact.alignment_sha256 != doc.alignment_sha256
            or bundle.questions_digest != doc.assignment_questions_digest
        ):
            raise AccessError("homework_visual_source_changed", 409)
    await verify_text(factory, storage, doc)
    async with db.begin():
        _, file, selection, regions = await checked_selection(
            db, principal, submission_id, question_id, doc.selection_id, lock=True
        )
        question = await db.scalar(select(Question).where(Question.id == question_id))
        answer = await db.scalar(select(AnswerKey).where(AnswerKey.question_id == question_id))
        rows = (
            await db.scalars(
                select(RubricCriterion)
                .where(RubricCriterion.question_id == question_id)
                .order_by(RubricCriterion.sort_order)
            )
        ).all()
        questions = (
            await db.scalars(
                select(Question)
                .where(Question.assignment_id == doc.assignment_id)
                .order_by(Question.question_number, Question.id)
            )
        ).all()
        rubric = [
            RubricCriterionSnapshot(
                criterion_id=r.id,
                question_id=r.question_id,
                description=r.description,
                points=r.points,
                sort_order=r.sort_order,
            )
            for r in rows
        ]
        if (
            selection["regions_sha256"] != doc.selection_sha256
            or file["sha256"] != doc.source_file_sha256
            or not question
            or not answer
            or question.question_number != doc.question.question_number
            or question.question_type != doc.question.question_type
            or question.stem != doc.question.stem
            or question.max_score != doc.question.max_score
            or answer.id != doc.answer_key.answer_key_id
            or answer.source != "teacher"
            or hashlib.sha256(answer.answer_content.encode()).hexdigest() != doc.answer_key.sha256
            or rubric_sha256(rubric) != doc.rubric_sha256
            or assignment_questions_digest(questions) != doc.assignment_questions_digest
        ):
            raise AccessError("homework_visual_source_changed", 409)
    await materialize(storage, file, regions)


async def score(
    db, principal, storage, submission_id, question_id, request, *, config=None, provider=None
):
    config = config or settings
    if not config.grading_enabled or not config.grading_visual_enabled:
        raise AccessError("homework_visual_grading_disabled", 409)
    digest = json_sha256(
        dict(
            submission_id=str(submission_id),
            question_id=str(question_id),
            request=request.model_dump(mode="json"),
        )
    )
    async with db.begin():
        attempt, file, selection, regions = await checked_selection(
            db, principal, submission_id, question_id, write=True, lock=True
        )
        old = (
            (
                await db.execute(
                    select(J).where(
                        J.c.actor_user_id == principal.user_id, J.c.request_id == request.request_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if old:
            if old["request_sha256"] != digest or old["selection_id"] != selection["id"]:
                raise AccessError("homework_visual_request_conflict", 409)
            return {"job_id": str(old["id"]), "status": old["status"], "created": False}
        if selection["revision"] != request.expected_selection_revision:
            raise AccessError("homework_visual_revision_conflict", 409)
        if await db.scalar(select(J.c.id).where(J.c.selection_id == selection["id"])):
            raise AccessError("homework_visual_selection_already_scored", 409)
        attempt_id = attempt["id"]
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    core = await build_core(
        db,
        factory,
        storage,
        principal,
        submission_id,
        question_id,
        selection,
        regions,
        file,
        request,
    )
    images = await materialize(storage, file, regions)
    document = HomeworkVisualInput(
        **core.model_dump(),
        artifact_id=uuid4(),
        input_sha256=json_sha256(core),
        created_at=datetime.now(UTC),
    )
    stored = await store_document(
        storage, "input", document.artifact_id, document, config.grade_input_max_document_bytes
    )
    async with db.begin():
        current, _, selected, _ = await checked_selection(
            db, principal, submission_id, question_id, selection["id"], write=True, lock=True
        )
        if current["id"] != attempt_id:
            raise AccessError("homework_visual_source_changed", 409)
        old = (
            (
                await db.execute(
                    select(J).where(
                        J.c.actor_user_id == principal.user_id, J.c.request_id == request.request_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if old:
            if old["request_sha256"] != digest or old["selection_id"] != selected["id"]:
                raise AccessError("homework_visual_request_conflict", 409)
            return {"job_id": str(old["id"]), "status": old["status"], "created": False}
        if await db.scalar(select(J.c.id).where(J.c.selection_id == selected["id"])):
            raise AccessError("homework_visual_selection_already_scored", 409)
        await db.execute(
            insert(GI).values(
                id=document.artifact_id,
                request_id=document.request_id,
                submission_id=document.submission_id,
                assignment_id=document.assignment_id,
                question_id=document.question.question_id,
                question_number=document.question.question_number,
                canonical_artifact_id=document.canonical_artifact_id,
                canonical_sha256=document.canonical_sha256,
                alignment_artifact_id=document.alignment_artifact_id,
                alignment_sha256=document.alignment_sha256,
                assignment_questions_digest=document.assignment_questions_digest,
                review_decision_id=None,
                visual_selection_id=None,
                homework_visual_selection_id=document.selection_id,
                review_revision=document.selection_revision,
                review_request_sha256=document.selection_sha256,
                source_file_sha256=document.source_file_sha256,
                answer_key_id=document.answer_key.answer_key_id,
                answer_key_sha256=document.answer_key.sha256,
                rubric_sha256=document.rubric_sha256,
                input_sha256=document.input_sha256,
                schema_version="hw-visual-1",
                **stored,
            )
        )
        job_id = uuid4()
        await db.execute(
            insert(J).values(
                id=job_id,
                selection_id=selected["id"],
                actor_user_id=principal.user_id,
                request_id=request.request_id,
                request_sha256=digest,
                status="running",
            )
        )
    try:
        await verify_text(factory, storage, document)
        selected_provider = provider or build_text_grading_provider(config)
        proposal = await visual_scoring.make_proposal(
            selected_provider, document, images, job_id, config, prompt_version=PROMPT_VERSION
        )
        await verify_document(db, factory, storage, principal, submission_id, question_id, document)
        stored_proposal = await store_document(
            storage, "proposal", proposal.proposal_id, proposal, config.grading_max_proposal_bytes
        )
        async with db.begin():
            current, _, _, _ = await checked_selection(
                db, principal, submission_id, question_id, selection["id"], write=True, lock=True
            )
            if current["id"] != attempt_id:
                raise AccessError("homework_visual_source_changed", 409)
            values = proposal.model_dump(
                exclude={
                    "schema_name",
                    "schema_version",
                    "criterion_scores",
                    "total_score",
                    "max_score",
                    "reason_codes",
                    "abstention_reason",
                },
                mode="python",
            )
            values["id"] = values.pop("proposal_id")
            values.update(**stored_proposal)
            await db.execute(insert(GP).values(**values))
            changed = await db.execute(
                update(J)
                .where(J.c.id == job_id, J.c.status == "running")
                .values(status="succeeded", proposal_id=proposal.proposal_id)
            )
            if changed.rowcount != 1:
                raise AccessError("homework_visual_job_changed", 409)
        return {"job_id": str(job_id), "status": "succeeded", "created": True}
    except asyncio.CancelledError:
        # The remote request may still be in flight. Do not retry or claim failure.
        raise
    except Exception as error:
        try:
            async with db.begin():
                await db.execute(
                    update(J)
                    .where(J.c.id == job_id, J.c.status == "running")
                    .values(
                        status="invalidated" if isinstance(error, AccessError) else "failed",
                        failure_code=(
                            "source_changed"
                            if isinstance(error, AccessError)
                            else error.status
                            if isinstance(error, GradeProposalError)
                            else "scoring_failed"
                        ),
                    )
                )
        except Exception:
            pass  # Unknown COMMIT: leave the immutable attempt for reconciliation.
        if isinstance(error, AccessError):
            raise
        raise AccessError("homework_visual_scoring_failed", 503) from None


async def details(db, principal, storage, submission_id, question_id):
    """Only expose the latest teacher decision for the current authorized attempt."""
    async with db.begin():
        attempt, _ = await context(db, principal, submission_id, question_id, lock=True)
        selection = await latest_selection(db, attempt["id"], question_id)
        if not selection:
            return {"selection": None, "job": None, "proposal": None, "review": None}
        await checked_selection(db, principal, submission_id, question_id, selection["id"])
        job = (
            (await db.execute(select(J).where(J.c.selection_id == selection["id"])))
            .mappings()
            .first()
        )
        result = {
            "selection": {
                "id": str(selection["id"]),
                "revision": selection["revision"],
                "regions": selection["regions"],
            },
            "job": None
            if not job
            else {
                "id": str(job["id"]),
                "status": job["status"],
                "failure_code": job["failure_code"],
            },
            "proposal": None,
            "review": None,
        }
        if not job or job["status"] != "succeeded":
            return result
        scorer = await actor(db, job["actor_user_id"])
        await context(db, scorer, submission_id, question_id, write=True)
        proposal_row = (
            (await db.execute(select(GP).where(GP.c.id == job["proposal_id"]))).mappings().one()
        )
        input_row = (
            (await db.execute(select(GI).where(GI.c.id == proposal_row["grade_input_artifact_id"])))
            .mappings()
            .one()
        )
        last = (
            (
                await db.execute(
                    select(GR)
                    .where(GR.c.proposal_id == job["proposal_id"])
                    .order_by(GR.c.review_revision.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
    doc = await read_document(
        storage, input_row, HomeworkVisualInput, settings.grade_input_max_document_bytes
    )
    proposal = await read_document(
        storage, proposal_row, VisualProposal, settings.grading_max_proposal_bytes
    )
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    await verify_document(db, factory, storage, principal, submission_id, question_id, doc)
    try:
        visual_scoring.validate_binding(proposal, doc, settings)
    except (GradeReviewError, GradeProposalError):
        raise AccessError("homework_visual_document_invalid", 409) from None
    async with db.begin():
        await checked_selection(
            db, principal, submission_id, question_id, selection["id"], lock=True
        )
        await context(
            db, await actor(db, job["actor_user_id"]), submission_id, question_id, write=True
        )
    result["proposal"] = proposal.model_dump(mode="json")
    if last:
        result["review"] = {
            "review_id": str(last["review_id"]),
            "revision": last["review_revision"],
            "status": last["review_status"],
            "reviewed_total_score": (
                str(last["reviewed_total_score"])
                if last["reviewed_total_score"] is not None
                else None
            ),
        }
    return result


async def record_review(db, principal, storage, submission_id, question_id, request):
    value = await details(db, principal, storage, submission_id, question_id)
    if not value["proposal"] or not value["job"]:
        raise AccessError("homework_visual_proposal_unavailable", 409)
    proposal = VisualProposal.model_validate(value["proposal"])
    async with db.begin():
        job = (
            (await db.execute(select(J).where(J.c.id == UUID(value["job"]["id"])))).mappings().one()
        )
        input_row = (
            (await db.execute(select(GI).where(GI.c.id == proposal.grade_input_artifact_id)))
            .mappings()
            .one()
        )
        proposal_row = (
            (await db.execute(select(GP).where(GP.c.id == proposal.proposal_id))).mappings().one()
        )
    doc = await read_document(
        storage, input_row, HomeworkVisualInput, settings.grade_input_max_document_bytes
    )
    try:
        payload = VisualReviewDecision(
            schema_name="huipi.grading.visual-review",
            schema_version="2.0",
            reviewer_ref=f"teacher_{principal.user_id.hex}",
            review_status=request.review_status,
            review_reason_codes=(
                ["candidate_not_reliable"]
                if request.review_status in {"rejected", "regrading_required"}
                else []
            ),
            review_comment=request.review_comment,
            criterion_reviews=request.criterion_reviews,
        )
    except ValidationError:
        raise AccessError("homework_visual_review_invalid", 422) from None
    try:
        criteria, original, total = review_service._build_review_criteria(
            proposal,
            doc,
            payload,
            settings,
            evidence_validator=visual_scoring.validate_evidence,
            criterion_model=VisualReviewCriterion,
        )
    except GradeReviewError as error:
        raise AccessError(error.reason_code, 422) from None
    digest = grade_review_request_sha256(
        proposal_id=proposal.proposal_id,
        request_id=request.request_id,
        expected_revision=request.expected_revision,
        payload=payload,
        reviewer_user_id=principal.user_id,
    )
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    await verify_document(db, factory, storage, principal, submission_id, question_id, doc)
    async with db.begin():
        await checked_selection(
            db, principal, submission_id, question_id, doc.selection_id, write=True, lock=True
        )
        await context(
            db,
            await actor(db, job["actor_user_id"]),
            submission_id,
            question_id,
            write=True,
            lock=True,
        )
        old = (
            (
                await db.execute(
                    select(GR).where(
                        GR.c.proposal_id == proposal.proposal_id,
                        GR.c.request_id == request.request_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if old:
            if old["request_sha256"] != digest:
                raise AccessError("homework_visual_request_conflict", 409)
            return {"review_id": str(old["review_id"]), "revision": old["review_revision"]}
        previous = (
            (
                await db.execute(
                    select(GR)
                    .where(GR.c.proposal_id == proposal.proposal_id)
                    .order_by(GR.c.review_revision.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        revision = previous["review_revision"] if previous else 0
        if revision != request.expected_revision:
            raise AccessError("homework_visual_revision_conflict", 409)
        record = VisualReview(
            review_id=uuid4(),
            proposal_id=proposal.proposal_id,
            grade_input_artifact_id=doc.artifact_id,
            proposal_sha256=proposal_row["object_sha256"],
            grade_input_sha256=doc.input_sha256,
            submission_id=submission_id,
            question_id=question_id,
            review_revision=revision + 1,
            supersedes_review_id=previous["review_id"] if previous else None,
            reviewer_ref=payload.reviewer_ref,
            request_id=request.request_id,
            request_sha256=digest,
            review_status=payload.review_status,
            criterion_reviews=criteria,
            review_reason_codes=payload.review_reason_codes,
            review_comment=payload.review_comment,
            proposed_total_score=original,
            reviewed_total_score=total,
            created_at=datetime.now(UTC),
        )
        values = record.model_dump(exclude={"schema_name", "schema_version"}, mode="python")
        values["review_schema_version"] = record.schema_version
        values["reviewer_user_id"] = principal.user_id
        values["criterion_reviews"] = [r.model_dump(mode="json") for r in criteria]
        await db.execute(insert(GR).values(**values))
        return {"review_id": str(record.review_id), "revision": revision + 1}
