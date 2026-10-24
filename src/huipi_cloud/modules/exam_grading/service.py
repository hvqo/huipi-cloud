"""Fixed task-bound Core persistence; no protected ORM visibility switch exists."""

import asyncio
import hashlib
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace as NS
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import distinct_on

from huipi_cloud.core.config import settings
from huipi_cloud.modules.answer_alignment.matcher import assignment_questions_digest
from huipi_cloud.modules.answer_alignment.models import AnswerAlignmentArtifact
from huipi_cloud.modules.answer_alignment.protocol import ALIGNER_VERSION, AnswerAlignmentDocument
from huipi_cloud.modules.answer_alignment.repository import AlignmentContext
from huipi_cloud.modules.answer_alignment.segmenter import align_canonical_document
from huipi_cloud.modules.answer_alignment.service import VerifiedAlignmentBundle
from huipi_cloud.modules.answer_review import service as answers
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.assignments.models import AnswerKey, Assignment, Question, RubricCriterion
from huipi_cloud.modules.canonical_documents.protocol import CanonicalDocument
from huipi_cloud.modules.canonical_documents.service import _read_checked_object
from huipi_cloud.modules.exam_parsing import service as parsing
from huipi_cloud.modules.grading import grade_review_service as reviews
from huipi_cloud.modules.grading import service as inputs
from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision
from huipi_cloud.modules.grading.grade_review_protocol import (
    GradeReviewRevisionDocument,
    grade_review_request_sha256,
)
from huipi_cloud.modules.grading.models import GradeInputArtifact, GradeProposalArtifact
from huipi_cloud.modules.grading.proposal_protocol import GradeProposalDocument
from huipi_cloud.modules.grading.protocol import (
    GradeInputDocument,
    canonical_json_bytes,
    json_sha256,
)
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.papers.models import PaperQuestionMapping
from huipi_cloud.modules.scans import service as scans
from huipi_cloud.modules.submissions.models import SubmissionFile

from .models import ExamGradingOperation, ExamGradingWorkspace, ExamScoringJob

W, S, OPS, AL, AR, GI, GP, GR, QM, Q, AK, RU, AS, F = (
    x.__table__
    for x in (
        ExamGradingWorkspace,
        ExamScoringJob,
        ExamGradingOperation,
        AnswerAlignmentArtifact,
        AnswerReviewDecision,
        GradeInputArtifact,
        GradeProposalArtifact,
        GradeReviewRevision,
        PaperQuestionMapping,
        Question,
        AnswerKey,
        RubricCriterion,
        Assignment,
        SubmissionFile,
    )
)


def obj(value):
    return NS(**dict(value)) if value is not None else None


async def row(db, table, identifier):
    value = (
        (await db.execute(select(table).where(list(table.primary_key.columns)[0] == identifier)))
        .mappings()
        .first()
    )
    if value is None:
        raise AccessError("resource_not_found_or_forbidden", 404)
    return value


async def context(db, principal, jid, action="exam.mark.read"):
    job = await row(db, parsing.J, jid)
    root = await row(db, parsing.scripts.A, job["answer_script_id"])
    await parsing.scripts.authorize(
        db,
        principal,
        root["exam_session_id"],
        "exam.mark.read",
        action,
        "exam.script.read",
        "exam.identity.verify",
    )
    await parsing.context(
        db,
        principal,
        root["id"],
        version_id=job["answer_script_version_id"],
        action="exam.mark.read",
    )
    # The original parsing intent remains current; a newer viewer cannot revive it.
    source = await parsing.context(
        db,
        await parsing.actor(db, job["requested_by_user_id"]),
        root["id"],
        version_id=job["answer_script_version_id"],
    )
    if job["status"] != "succeeded":
        raise AccessError("exam_mark_parse_not_ready", 409)
    link = await row(db, parsing.K, job["id"])
    ca = await row(db, parsing.CA, job["canonical_artifact_id"])
    mappings = (
        (
            await db.execute(
                select(QM)
                .where(QM.c.paper_version_id == link["paper_version_id"])
                .order_by(QM.c.sort_order)
                .limit(101)
            )
        )
        .mappings()
        .all()
    )
    if not mappings or len(mappings) > 100:
        raise AccessError("exam_mark_question_limit", 409)
    questions = (
        (
            await db.execute(
                select(Q)
                .where(
                    Q.c.id.in_([m["legacy_question_id"] for m in mappings]),
                    Q.c.assignment_id == source[4]["canonical_assignment_id"],
                )
                .order_by(Q.c.question_number, Q.c.id)
            )
        )
        .mappings()
        .all()
    )
    if len(questions) != len(mappings):
        raise AccessError("exam_mark_question_mismatch", 409)
    return NS(
        job=job,
        link=link,
        canonical=ca,
        source=source,
        mappings=mappings,
        questions=[obj(q) for q in questions],
    )


async def workspace_context(db, principal, wid, action="exam.mark.read"):
    w = await row(db, W, wid)
    c = await context(db, principal, w["parsing_job_id"], action)
    mapping = next((m for m in c.mappings if m["id"] == w["paper_question_mapping_id"]), None)
    if mapping is None or mapping["legacy_question_id"] != w["question_id"]:
        raise AccessError("exam_mark_source_changed", 409)
    c.workspace = w
    c.question = next(q for q in c.questions if q.id == w["question_id"])
    return c


async def audit(db, p, jid, rid, action, digest, result):
    await db.execute(
        insert(OPS).values(
            id=uuid4(),
            parsing_job_id=jid,
            actor_user_id=p.user_id,
            request_id=rid,
            action=action,
            payload_sha256=digest,
            result=result,
        )
    )


async def replay(db, p, rid, digest):
    # Caller must already hold current authority/source locks, including on replay.
    await db.execute(
        select(
            func.pg_advisory_xact_lock(
                int.from_bytes(
                    hashlib.sha256(f"exam-mark:{p.user_id}:{rid}".encode()).digest()[:8],
                    signed=True,
                )
            )
        )
    )
    old = (
        (
            await db.execute(
                select(OPS).where(OPS.c.actor_user_id == p.user_id, OPS.c.request_id == rid)
            )
        )
        .mappings()
        .first()
    )
    if old and old["payload_sha256"] != digest:
        raise AccessError("exam_mark_request_conflict", 409)
    return old["result"] if old else None


async def upload(storage, kind, identifier, document, maximum):
    body = canonical_json_bytes(document)
    if len(body) > maximum:
        raise AccessError("exam_mark_size_limit", 413)
    sha = hashlib.sha256(body).hexdigest()
    key = f"{storage.object_key_prefix}/exam-mark/{kind}/{identifier}/{uuid4()}.json"
    async with asyncio.timeout(60):
        await storage.upload_fileobj(BytesIO(body), key, "application/json")
        await _read_checked_object(
            storage, key, expected_size=len(body), expected_sha256=sha, maximum=maximum
        )
    # Never delete on unknown COMMIT; unique losing attempts are delayed reconciliation work.
    return dict(
        bucket=storage.bucket, object_key=key, object_sha256=sha, object_size_bytes=len(body)
    )


async def read_document(storage, record, model, maximum, *, alignment=False):
    if record["bucket"] != storage.bucket:
        raise AccessError("exam_mark_storage_unavailable", 503)
    async with asyncio.timeout(60):
        body = await _read_checked_object(
            storage,
            record["object_key"],
            expected_size=record["alignment_size_bytes"]
            if alignment
            else record["object_size_bytes"],
            expected_sha256=record["alignment_sha256"] if alignment else record["object_sha256"],
            maximum=maximum,
        )
    return model.model_validate_json(body.data)


async def initialize(db, p, storage, jid, request, config=settings):
    digest = json_sha256(dict(action="align", job=str(jid), body=request.model_dump(mode="json")))
    async with scans.transaction(db):
        c = await context(db, p, jid, "exam.answer.review")
        old = await replay(db, p, request.request_id, digest)
        if old:
            return old
        existing = (
            (await db.execute(select(W.c.id).where(W.c.parsing_job_id == jid))).scalars().all()
        )
        if existing:
            result = {"workspace_ids": list(map(str, existing))}
            await audit(db, p, jid, request.request_id, "align", digest, result)
            return result
    await parsing.scripts.verify_sources(storage, c.source[2])
    ca = c.canonical
    if ca["bucket"] != storage.bucket:
        raise AccessError("exam_mark_storage_unavailable", 503)
    payload = await _read_checked_object(
        storage,
        ca["canonical_object_key"],
        expected_size=ca["canonical_size_bytes"],
        expected_sha256=ca["canonical_sha256"],
        maximum=config.canonical_max_document_bytes,
    )
    document = CanonicalDocument.model_validate_json(payload.data)
    if (
        document.document_id != ca["id"]
        or document.submission_id != c.link["submission_id"]
        or document.source_sha256 != ca["source_sha256"]
        or document.source_artifact_id != ca["parsed_artifact_id"]
    ):
        raise AccessError("exam_mark_source_changed", 409)
    digest_q = assignment_questions_digest(c.questions)
    alid = uuid5(ca["id"], f"1.0:{ALIGNER_VERSION}:{digest_q}")
    aligned = align_canonical_document(
        document,
        c.questions,
        assignment_id=c.source[4]["canonical_assignment_id"],
        canonical_sha256=ca["canonical_sha256"],
        alignment_id=alid,
    )
    stored = await upload(
        storage, "alignment", alid, aligned, config.answer_alignment_max_document_bytes
    )
    async with scans.transaction(db):
        current = await context(db, p, jid, "exam.answer.review")
        old = await replay(db, p, request.request_id, digest)
        if old:
            return old
        existing = (
            (await db.execute(select(W.c.id).where(W.c.parsing_job_id == jid))).scalars().all()
        )
        if not existing:
            await db.execute(
                insert(AL).values(
                    id=alid,
                    submission_id=c.link["submission_id"],
                    assignment_id=c.source[4]["canonical_assignment_id"],
                    canonical_artifact_id=ca["id"],
                    canonical_sha256=ca["canonical_sha256"],
                    aligner_version=ALIGNER_VERSION,
                    assignment_questions_digest=digest_q,
                    status=aligned.status,
                    bucket=stored["bucket"],
                    object_key=stored["object_key"],
                    alignment_sha256=stored["object_sha256"],
                    alignment_size_bytes=stored["object_size_bytes"],
                    question_count=len(aligned.answers),
                    aligned_count=sum(a.matching_status == "aligned" for a in aligned.answers),
                    review_required_count=sum(
                        a.matching_status == "review_required" for a in aligned.answers
                    ),
                    unmatched_count=sum(
                        a.matching_status == "unmatched" for a in aligned.candidates
                    ),
                    not_observed_count=sum(
                        a.matching_status == "not_observed" for a in aligned.answers
                    ),
                )
            )
            existing = []
            for mapping in current.mappings:
                wid = uuid5(jid, str(mapping["id"]))
                existing.append(wid)
                await db.execute(
                    insert(W).values(
                        id=wid,
                        parsing_job_id=jid,
                        paper_question_mapping_id=mapping["id"],
                        alignment_artifact_id=alid,
                        question_id=mapping["legacy_question_id"],
                    )
                )
        result = {"workspace_ids": list(map(str, existing))}
        await audit(db, p, jid, request.request_id, "align", digest, result)
        return result


async def bundle(db, p, storage, wid, action="exam.mark.read", config=settings):
    async with scans.transaction(db):
        c = await workspace_context(db, p, wid, action)
        al = await row(db, AL, c.workspace["alignment_artifact_id"])
    ca = c.canonical
    if ca["bucket"] != storage.bucket:
        raise AccessError("exam_mark_storage_unavailable", 503)
    payload = await _read_checked_object(
        storage,
        ca["canonical_object_key"],
        expected_size=ca["canonical_size_bytes"],
        expected_sha256=ca["canonical_sha256"],
        maximum=config.canonical_max_document_bytes,
    )
    canonical = CanonicalDocument.model_validate_json(payload.data)
    alignment = await read_document(
        storage,
        al,
        AnswerAlignmentDocument,
        config.answer_alignment_max_document_bytes,
        alignment=True,
    )
    if (
        canonical.document_id != ca["id"]
        or canonical.submission_id != c.link["submission_id"]
        or alignment.canonical_document_id != ca["id"]
        or alignment.canonical_sha256 != ca["canonical_sha256"]
        or alignment.assignment_questions_digest != assignment_questions_digest(c.questions)
    ):
        raise AccessError("exam_mark_source_changed", 409)
    c.bundle = VerifiedAlignmentBundle(
        AlignmentContext(
            c.link["submission_id"],
            c.source[4]["canonical_assignment_id"],
            "succeeded",
            obj(ca),
            c.questions,
        ),
        obj(ca),
        canonical,
        obj(al),
        alignment,
        assignment_questions_digest(c.questions),
    )
    return c


async def latest_answer(db, c):
    return (
        (
            await db.execute(
                select(AR)
                .where(
                    AR.c.submission_id == c.link["submission_id"], AR.c.question_id == c.question.id
                )
                .order_by(AR.c.revision.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )


async def assessment(db, c, rid, config=settings):
    assignment = await row(db, AS, c.source[4]["canonical_assignment_id"])
    ak = (await db.execute(select(AK).where(AK.c.question_id == c.question.id))).mappings().first()
    rubric = (
        (
            await db.execute(
                select(RU)
                .where(RU.c.question_id == c.question.id)
                .order_by(RU.c.sort_order, RU.c.id)
            )
        )
        .mappings()
        .all()
    )
    file = await row(db, F, c.link["submission_file_id"])
    return inputs.assess_verified_sources(
        bundle=c.bundle,
        question=c.question,
        assignment=obj(assignment),
        answer_key=obj(ak),
        rubric_rows=[obj(r) for r in rubric],
        source_file=obj(file),
        latest_review=obj(await latest_answer(db, c)),
        request_id=rid,
        config=config,
    )


async def confirm_answer(db, p, storage, wid, request, config=settings):
    if request.payload.reviewer_ref != f"teacher_{p.user_id.hex}":
        raise AccessError("exam_mark_reviewer_mismatch", 422)
    c = await bundle(db, p, storage, wid, "exam.answer.review", config)
    payload = request.payload
    nodes = answers._index_canonical_nodes(c.bundle.canonical_document)
    response = answers._materialize_regions(payload.response_regions, nodes)
    prompt = answers._materialize_regions(payload.excluded_prompt_regions, nodes)
    uncertain = answers._materialize_regions(
        payload.uncertain_regions, nodes, allow_block_asset_context=True
    )
    answers._validate_selection_conflicts(payload, nodes)
    answers._validate_question_ownership(
        payload,
        c.question.id,
        c.bundle.alignment_document.answers,
        ([] if request.manual_attribution else c.bundle.alignment_document.unassigned_regions),
        nodes,
    )
    digest = json_sha256(
        dict(action="answer", workspace=str(wid), body=request.model_dump(mode="json"))
    )
    async with scans.transaction(db):
        await workspace_context(db, p, wid, "exam.answer.review")
        old = await replay(db, p, request.request_id, digest)
        if old:
            return old
        latest = await latest_answer(db, c)
        revision = latest["revision"] if latest else 0
        if revision != request.expected_revision:
            raise AccessError("exam_mark_revision_conflict", 409)
        other_reviews = (
            (
                await db.execute(
                    select(AR)
                    .where(
                        AR.c.submission_id == c.link["submission_id"],
                        AR.c.question_id != c.question.id,
                    )
                    .ext(distinct_on(AR.c.question_id))
                    .order_by(AR.c.question_id, AR.c.revision.desc())
                    .limit(100)
                )
            )
            .mappings()
            .all()
        )
        for other in other_reviews:
            if other["decision"] != "response_present":
                continue
            for selection in payload.response_regions:
                if any(
                    answers._selection_overlaps_source(
                        selection, answers.ReviewEvidenceRegion.model_validate(region), nodes
                    )
                    for region in other["response_regions"]
                ):
                    raise AccessError("exam_mark_region_owned_by_other_question", 409)
        aid = uuid4()
        al = c.bundle.alignment_artifact
        await db.execute(
            insert(AR).values(
                id=aid,
                submission_id=c.link["submission_id"],
                assignment_id=c.source[4]["canonical_assignment_id"],
                question_id=c.question.id,
                question_number=c.question.question_number,
                alignment_status=next(
                    a.matching_status
                    for a in c.bundle.alignment_document.answers
                    if a.question_id == c.question.id
                ),
                alignment_artifact_id=al.id,
                alignment_sha256=al.alignment_sha256,
                aligner_version=al.aligner_version,
                assignment_questions_digest=al.assignment_questions_digest,
                canonical_document_id=c.canonical["id"],
                canonical_sha256=c.canonical["canonical_sha256"],
                review_schema_version="1.0",
                decision=payload.decision,
                response_regions=[r.model_dump(mode="json") for r in response],
                excluded_prompt_regions=[r.model_dump(mode="json") for r in prompt],
                uncertain_regions=[r.model_dump(mode="json") for r in uncertain],
                reason_codes=payload.reason_codes,
                reviewer_ref=payload.reviewer_ref,
                reviewer_user_id=p.user_id,
                request_id=request.request_id,
                request_sha256=digest,
                revision=revision + 1,
                supersedes_decision_id=latest["id"] if latest else None,
            )
        )
        result = dict(decision_id=str(aid), revision=revision + 1)
        await audit(db, p, c.job["id"], request.request_id, "answer", digest, result)
        return result


async def enqueue(db, p, storage, wid, request, config=settings):
    c = await bundle(db, p, storage, wid, "exam.score.request", config)
    digest = json_sha256(
        dict(action="score", workspace=str(wid), body=request.model_dump(mode="json"))
    )
    async with scans.transaction(db):
        await workspace_context(db, p, wid, "exam.score.request")
        old = await replay(db, p, request.request_id, digest)
        if old:
            return old
        latest = await latest_answer(db, c)
        if not latest or latest["revision"] != request.expected_answer_revision:
            raise AccessError("exam_mark_answer_changed", 409)
        existing = (
            (
                await db.execute(
                    select(S).where(S.c.workspace_id == wid, S.c.answer_review_id == latest["id"])
                )
            )
            .mappings()
            .first()
        )
        if existing:
            result = {"scoring_job_id": str(existing["id"])}
            await audit(db, p, c.job["id"], request.request_id, "score", digest, result)
            return result
        assessed = await assessment(db, c, request.request_id, config)
    if assessed.core is None:
        raise AccessError("exam_mark_" + assessed.inspection.status, 409)
    doc = GradeInputDocument(
        **assessed.core.model_dump(),
        artifact_id=uuid4(),
        input_sha256=json_sha256(assessed.core),
        created_at=datetime.now(UTC),
    )
    stored = await upload(
        storage, "input", doc.artifact_id, doc, config.grade_input_max_document_bytes
    )
    async with scans.transaction(db):
        await workspace_context(db, p, wid, "exam.score.request")
        old = await replay(db, p, request.request_id, digest)
        if old:
            return old
        latest = await latest_answer(db, c)
        if latest["id"] != doc.review_decision_id:
            raise AccessError("exam_mark_answer_changed", 409)
        existing = (
            (
                await db.execute(
                    select(S).where(S.c.workspace_id == wid, S.c.answer_review_id == latest["id"])
                )
            )
            .mappings()
            .first()
        )
        if existing:
            jid = existing["id"]
        else:
            vals = {
                k: getattr(doc, k)
                for k in (
                    "submission_id",
                    "assignment_id",
                    "canonical_artifact_id",
                    "canonical_sha256",
                    "alignment_artifact_id",
                    "alignment_sha256",
                    "assignment_questions_digest",
                    "review_revision",
                    "review_request_sha256",
                    "source_file_sha256",
                    "rubric_sha256",
                    "input_sha256",
                )
            }
            vals.update(
                id=doc.artifact_id,
                request_id=doc.request_id,
                question_id=doc.question.question_id,
                question_number=doc.question.question_number,
                review_decision_id=doc.review_decision_id,
                answer_key_id=doc.answer_key.answer_key_id,
                answer_key_sha256=doc.answer_key.sha256,
                schema_version=doc.schema_version,
                **stored,
            )
            await db.execute(insert(GI).values(**vals))
            jid = uuid4()
            await db.execute(
                insert(S).values(
                    id=jid,
                    workspace_id=wid,
                    answer_review_id=doc.review_decision_id,
                    grade_input_id=doc.artifact_id,
                    requested_by_user_id=p.user_id,
                )
            )
        result = {"scoring_job_id": str(jid)}
        await audit(db, p, c.job["id"], request.request_id, "score", digest, result)
        return result


async def scoring_context(db, p, job, action="exam.mark.read", config=settings):
    c = await workspace_context(db, p, job["workspace_id"], action)
    latest = await latest_answer(db, c)
    if latest is None or latest["id"] != job["answer_review_id"]:
        raise AccessError("exam_mark_answer_changed", 409)
    indexed = await row(db, GI, job["grade_input_id"])
    if (
        indexed["review_decision_id"] != latest["id"]
        or indexed["submission_id"] != c.link["submission_id"]
        or indexed["question_id"] != c.question.id
    ):
        raise AccessError("exam_mark_input_changed", 409)
    return c, indexed


async def read_input(storage, indexed, config=settings):
    doc = await read_document(
        storage, indexed, GradeInputDocument, config.grade_input_max_document_bytes
    )
    if (
        doc.artifact_id != indexed["id"]
        or doc.input_sha256 != indexed["input_sha256"]
        or doc.review_decision_id != indexed["review_decision_id"]
        or doc.question.question_id != indexed["question_id"]
    ):
        raise AccessError("exam_mark_input_changed", 409)
    return doc


async def details(db, p, storage, wid, config=settings):
    c = await bundle(db, p, storage, wid, config=config)
    async with scans.transaction(db):
        await workspace_context(db, p, wid)
        review = await latest_answer(db, c)
        ak = (
            (await db.execute(select(AK).where(AK.c.question_id == c.question.id)))
            .mappings()
            .first()
        )
        rubric = (
            (
                await db.execute(
                    select(RU).where(RU.c.question_id == c.question.id).order_by(RU.c.sort_order)
                )
            )
            .mappings()
            .all()
        )
        job = (
            (
                await db.execute(
                    select(S)
                    .where(S.c.workspace_id == wid)
                    .order_by(S.c.created_at.desc(), S.c.id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        history = []
        proposal = None
        inputdoc = None
        state = "not_started"
        if job:
            state = job["status"]
            try:
                await scoring_context(
                    db,
                    await parsing.actor(db, job["requested_by_user_id"]),
                    job,
                    "exam.score.request",
                    config,
                )
            except AccessError:
                state = "invalidated"
            if state == "succeeded":
                pr = await row(db, GP, job["proposal_id"])
                ir = await row(db, GI, job["grade_input_id"])
                history = (
                    (
                        await db.execute(
                            select(GR)
                            .where(GR.c.proposal_id == job["proposal_id"])
                            .order_by(GR.c.review_revision.desc())
                            .limit(20)
                        )
                    )
                    .mappings()
                    .all()
                )
    if job and state == "succeeded":
        proposal = await read_document(
            storage, pr, GradeProposalDocument, config.grading_max_proposal_bytes
        )
        inputdoc = await read_input(storage, ir, config)
        reviews._validate_proposal_binding(proposal, inputdoc, config)
        async with scans.transaction(db):
            await scoring_context(db, p, job)
            await scoring_context(
                db,
                await parsing.actor(db, job["requested_by_user_id"]),
                job,
                "exam.score.request",
                config,
            )
    nodes = []
    size = 0
    index = answers._index_canonical_nodes(c.bundle.canonical_document)
    for pointer, location in index.items():
        if location.node.content_kind != "scalar":
            continue
        value = location.node.value or ""
        size += len(value.encode())
        if len(nodes) >= 1000 or size > 1024**2:
            raise AccessError("exam_mark_text_limit", 413)
        nodes.append(
            dict(
                content_pointer=pointer,
                page_index=location.page_index,
                source_block_id=str(location.block.block_id),
                text=value,
                normalized_type=location.node.normalized_type,
            )
        )
    async with scans.transaction(db):
        await workspace_context(db, p, wid)
    result = dict(
        workspace_id=str(wid),
        question=dict(
            id=str(c.question.id),
            number=c.question.question_number,
            stem=c.question.stem,
            max_score=str(c.question.max_score),
        ),
        answer_key=ak["answer_content"] if ak else None,
        rubric=[
            dict(criterion_id=str(r["id"]), description=r["description"], points=str(r["points"]))
            for r in rubric
        ],
        nodes=nodes,
        source_pages=[parsing.scripts.public_page(r) for r in c.source[2]],
        alignment=next(
            a.model_dump(mode="json")
            for a in c.bundle.alignment_document.answers
            if a.question_id == c.question.id
        ),
        answer_review=answers.decision_read(obj(review)).model_dump(mode="json")
        if review
        else None,
        scoring_job_id=str(job["id"]) if job else None,
        scoring_status=state,
        failure_code=job["failure_code"] if job else None,
        proposal=proposal.model_dump(mode="json") if proposal else None,
        grade_input=inputdoc.model_dump(mode="json") if inputdoc else None,
        history=[
            reviews._row_to_document(obj(r)).model_dump(mode="json") for r in reversed(history)
        ],
        history_has_more=bool(history and history[-1]["review_revision"] > 1),
    )

    from .schemas import ExamMarkDetails

    result = ExamMarkDetails.model_validate(result).model_dump(mode="json")
    if len(canonical_json_bytes(result)) > 2 * 1024**2:
        raise AccessError("exam_mark_response_limit", 413)
    return result


async def list_workspaces(db, p, jid):
    async with scans.transaction(db):
        c = await context(db, p, jid)
        workspaces = (await db.execute(select(W).where(W.c.parsing_job_id == jid))).mappings().all()
        byq = {w["question_id"]: w for w in workspaces}
        return dict(
            questions=[
                dict(
                    question_id=str(q.id),
                    question_number=q.question_number,
                    stem=q.stem,
                    max_score=str(q.max_score),
                    workspace_id=str(byq[q.id]["id"]) if q.id in byq else None,
                )
                for q in c.questions
            ]
        )


async def record_score_review(db, p, storage, wid, request, config=settings):
    if request.payload.reviewer_ref != f"teacher_{p.user_id.hex}":
        raise AccessError("exam_mark_reviewer_mismatch", 422)
    # Reuse strict proposal/input reading and existing complete-snapshot review rules.
    value = await details(db, p, storage, wid, config)
    if not value["proposal"] or not value["grade_input"]:
        raise AccessError("exam_mark_proposal_unavailable", 409)
    proposal = GradeProposalDocument.model_validate(value["proposal"])
    doc = GradeInputDocument.model_validate(value["grade_input"])
    criteria, original, total = reviews._build_review_criteria(
        proposal, doc, request.payload, config
    )
    digest = grade_review_request_sha256(
        proposal_id=proposal.proposal_id,
        request_id=request.request_id,
        expected_revision=request.expected_revision,
        payload=request.payload,
        reviewer_user_id=p.user_id,
    )
    async with scans.transaction(db):
        c = await workspace_context(db, p, wid, "exam.score.review")
        job = await row(db, S, UUID(value["scoring_job_id"]))
        await scoring_context(db, p, job, "exam.score.review", config)
        await scoring_context(
            db,
            await parsing.actor(db, job["requested_by_user_id"]),
            job,
            "exam.score.request",
            config,
        )
        old = await replay(db, p, request.request_id, digest)
        if old:
            return old
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
            raise AccessError("exam_mark_revision_conflict", 409)
        record = GradeReviewRevisionDocument(
            review_id=uuid4(),
            proposal_id=proposal.proposal_id,
            grade_input_artifact_id=doc.artifact_id,
            proposal_sha256=(await row(db, GP, proposal.proposal_id))["object_sha256"],
            grade_input_sha256=doc.input_sha256,
            submission_id=doc.submission_id,
            question_id=doc.question.question_id,
            review_revision=revision + 1,
            supersedes_review_id=previous["review_id"] if previous else None,
            reviewer_ref=request.payload.reviewer_ref,
            request_id=request.request_id,
            request_sha256=digest,
            review_status=request.payload.review_status,
            criterion_reviews=criteria,
            review_reason_codes=request.payload.review_reason_codes,
            review_comment=request.payload.review_comment,
            proposed_total_score=original,
            reviewed_total_score=total,
            created_at=datetime.now(UTC),
        )
        vals = record.model_dump(exclude={"schema_name", "schema_version"}, mode="python")
        vals["review_schema_version"] = record.schema_version
        vals["reviewer_user_id"] = p.user_id
        vals["criterion_reviews"] = [r.model_dump(mode="json") for r in criteria]
        await db.execute(insert(GR).values(**vals))
        result = dict(review_id=str(record.review_id), revision=revision + 1)
        await audit(db, p, c.job["id"], request.request_id, "grade_review", digest, result)
        return result


async def http_authorize(db, p, params, action):
    if params.get("workspace_id"):
        w = await row(db, W, UUID(str(params["workspace_id"])))
        jid = w["parsing_job_id"]
    else:
        jid = UUID(str(params["job_id"]))
    await context(db, p, jid, action)


async def enable_operator(db, actor_id, target_id, request_id, approval_ref):
    """Fixed deployment root; database access and existing delegation authority required."""
    from huipi_cloud.modules.identity.models import User
    from huipi_cloud.modules.resource_security import service as security
    from huipi_cloud.modules.resource_security.models import SecurityGrant

    from .permissions import MARK_DEPLOYER_ROLE

    if not approval_ref.strip() or len(approval_ref) > 120:
        raise AccessError("exam_mark_approval_required", 422)
    p = await parsing.actor(db, actor_id)
    await db.rollback()
    async with security.operation(
        db,
        p,
        request_id,
        "bootstrap",
        dict(target=str(target_id), approval_ref=approval_ref, role="exam_mark_deployer"),
    ) as op:
        scope = await security.installation_scope(db)
        await security.require_permission(db, p, "security.grants.manage", scope.id)
        if op.replay is not None:
            return op.replay
        if (
            await db.scalar(
                select(User.id)
                .where(User.id == target_id, User.is_active)
                .with_for_update(read=True)
            )
            is None
        ):
            raise AccessError("resource_not_found_or_forbidden", 404)
        gid = uuid4()
        await db.execute(
            insert(SecurityGrant).values(
                id=gid,
                user_id=target_id,
                role_version_id=UUID(MARK_DEPLOYER_ROLE),
                scope_id=scope.id,
            )
        )
        op.result = dict(grant_id=str(gid))
        return op.result
