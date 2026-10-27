"""Fixed protected projections, one bounded consistent database snapshot per response."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.core.config import settings
from huipi_cloud.modules.exam_grading import service as g
from huipi_cloud.modules.grading.grade_review_errors import GradeReviewError
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError
from huipi_cloud.modules.grading.protocol import json_sha256
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.resource_security import service as security

from . import access
from .schemas import Preview, QuestionResult, StudentResult

scripts = g.parsing.scripts
MAX_EXPORT_STUDENTS = 100
MAX_CELLS = 2000


@asynccontextmanager
async def snapshot(db):
    try:
        async with asyncio.timeout(60), db.begin():
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            await db.execute(text("SET LOCAL statement_timeout='8000ms'"))
            await db.execute(text("SET LOCAL lock_timeout='5000ms'"))
            yield
    except (SQLAlchemyError, OSError, TimeoutError):
        raise AccessError("result_preview_unavailable", 503) from None


class BudgetStorage:
    """No unbounded response reads, including source-byte verification across a report."""

    def __init__(self, base):
        self.base, self.read_bytes = base, 0
        self.bucket = base.bucket

    async def download(self, key):
        source = await self.base.download(key)
        owner = self

        class Download:
            async def chunks(self, chunk_size=64 * 1024):
                iterator = source.chunks(chunk_size)
                try:
                    async for chunk in iterator:
                        owner.read_bytes += len(chunk)
                        if owner.read_bytes > 128 * 1024**2:
                            raise AccessError("result_source_read_limit", 413)
                        yield chunk
                finally:
                    await iterator.aclose()

        return Download()


async def sessions(db, p, event_id=None, offset=0, limit=20):
    async with snapshot(db):
        await security.identity.refresh_principal(db, p, lock=True)
        query = (
            select(access.C.c.exam_session_id)
            .distinct()
            .join(access.G, access.G.c.id == access.C.c.grant_id)
            .where(
                access.G.c.user_id == p.user_id,
                access.G.c.revoked_at.is_(None),
                access.C.c.revoked_at.is_(None),
            )
        )
        if event_id:
            query = query.join(scripts.S, scripts.S.c.id == access.C.c.exam_session_id).where(
                scripts.S.c.exam_event_id == event_id
            )
        ids = list(
            (
                await db.scalars(
                    query.order_by(access.C.c.exam_session_id).offset(offset).limit(limit + 1)
                )
            ).all()
        )
        items = []
        for sid in ids[:limit]:
            try:
                classes = await access.authorize(db, p, sid)
            except AccessError as e:
                if e.status_code == 404:
                    continue
                raise
            session = await g.row(db, scripts.S, sid)
            event = await g.row(db, scripts.EV, session["exam_event_id"])
            names = (
                await db.execute(
                    select(access.E.c.administrative_class_id, access.E.c.class_name)
                    .join(access.R, access.R.c.id == access.E.c.roster_revision_id)
                    .where(
                        access.R.c.exam_session_id == sid,
                        access.R.c.is_current,
                        access.E.c.administrative_class_id.in_(classes),
                    )
                    .distinct()
                )
            ).all()
            items.append(
                dict(
                    session_id=sid,
                    event_id=event["id"],
                    event_title=event["title"],
                    title=session["title"],
                    classes=[dict(class_id=c, class_name=n) for c, n in names],
                )
            )
        return dict(items=items, truncated=len(ids) > limit)


async def paper_questions(db, sid, paper_id):
    paper = await g.row(db, scripts.SP, paper_id)
    version = await g.row(db, scripts.PV, paper["paper_version_id"])
    if (
        paper["exam_session_id"] != sid
        or paper["status"] != "active"
        or version["status"] != "locked"
    ):
        raise AccessError("result_paper_unavailable", 409)
    if (
        paper["content_sha256"] != version["content_sha256"]
        or paper["mapping_digest"] != version["mapping_digest"]
    ):
        raise AccessError("result_paper_changed", 409)
    mappings = (
        (
            await db.execute(
                select(g.QM, g.Q.c.question_number, g.Q.c.max_score)
                .join(g.Q, g.Q.c.id == g.QM.c.legacy_question_id)
                .where(
                    g.QM.c.paper_version_id == version["id"],
                    g.Q.c.assignment_id == version["canonical_assignment_id"],
                )
                .order_by(g.QM.c.sort_order, g.QM.c.id)
                .limit(101)
            )
        )
        .mappings()
        .all()
    )
    if not mappings or len(mappings) > 100:
        raise AccessError("result_question_set_unavailable", 409)
    rubric = (
        (
            await db.execute(
                select(g.RU).where(
                    g.RU.c.question_id.in_([m["legacy_question_id"] for m in mappings])
                )
            )
        )
        .mappings()
        .all()
    )
    by_question = {}
    for rule in rubric:
        by_question.setdefault(rule["question_id"], {})[rule["id"]] = rule["points"]
    for mapping in mappings:
        rules = by_question.get(mapping["legacy_question_id"], {})
        if not rules or sum(rules.values(), Decimal("0.00")) != mapping["max_score"]:
            raise AccessError("result_rubric_unavailable", 409)
    return paper, version, mappings, by_question


async def question_result(db, p, storage, c, workspace, mapping, rules, config):
    result = QuestionResult(
        mapping_id=mapping["id"],
        question_id=mapping["legacy_question_id"],
        number=mapping["question_number"],
        max_points=str(sum(rules.values(), Decimal("0.00"))),
        status="not_scored",
        workspace_id=workspace["id"] if workspace else None,
    )
    if not workspace:
        return result
    job = (
        (
            await db.execute(
                select(g.S)
                .where(g.S.c.workspace_id == workspace["id"])
                .order_by(g.S.c.created_at.desc(), g.S.c.id.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    if not job:
        return result
    try:
        _, indexed = await g.scoring_context(db, p, job, config=config)
        await g.scoring_context(
            db,
            await g.parsing.actor(db, job["requested_by_user_id"]),
            job,
            "exam.score.request",
            config,
        )
        if job["status"] != "succeeded":
            result.reason = job["failure_code"] or job["status"]
            if job["status"] == "invalidated":
                result.status = "source_stale"
            return result
        pr = await g.row(db, g.GP, job["proposal_id"])
        doc = await g.read_input(storage, indexed, config)
        proposal = await g.read_document(
            storage, pr, g.GradeProposalDocument, config.grading_max_proposal_bytes
        )
        g.reviews._validate_proposal_binding(proposal, doc, config)
        if {r.criterion_id: r.points for r in doc.rubric_snapshot} != rules:
            raise AccessError("result_rubric_changed", 409)
        result.proposal_id = proposal.proposal_id
        if proposal.grading_status == "grading_abstained":
            result.status = "abstained"
            return result
        review = (
            (
                await db.execute(
                    select(g.GR)
                    .where(g.GR.c.proposal_id == proposal.proposal_id)
                    .order_by(g.GR.c.review_revision.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        if review is None:
            result.status = "awaiting_review"
            return result
        value = g.reviews._row_to_document(g.obj(review))
        if (
            value.proposal_sha256 != pr["object_sha256"]
            or value.grade_input_sha256 != doc.input_sha256
            or value.grade_input_artifact_id != doc.artifact_id
            or value.question_id != result.question_id
            or value.submission_id != doc.submission_id
        ):
            raise AccessError("result_review_changed", 409)
        result.review_id, result.review_revision = value.review_id, value.review_revision
        result.review_status = value.review_status
        if value.review_status in {"rejected", "regrading_required"}:
            result.status = value.review_status
            return result
        actual = {r.criterion_id: r for r in value.criterion_reviews}
        proposed = {r.criterion_id: r for r in proposal.criterion_scores}
        if set(actual) != set(rules):
            raise AccessError("result_review_incomplete", 409)
        for identifier, item in actual.items():
            if (
                item.max_points != rules[identifier]
                or item.proposed_points != proposed[identifier].awarded_points
            ):
                raise AccessError("result_review_changed", 409)
            g.reviews._validate_score_evidence(
                item.reviewed_points, item.evidence_references, doc, config
            )
        from huipi_cloud.modules.marking_tasks.access import result_valid

        if not await result_valid(db, workspace["id"], value.review_id):
            result.status, result.reason = (
                "awaiting_review",
                "marking_task_not_submitted_or_changed",
            )
            return result
        result.points = str(value.reviewed_total_score)
        result.status = "reviewed"
        return result
    except (AccessError, GradeProposalError, GradeReviewError) as error:
        if isinstance(error, AccessError) and error.status_code in {413, 503}:
            raise
        result.status, result.points = "source_stale", None
        result.reason = getattr(
            error, "code", getattr(error, "reason_code", "source_or_authority_changed")
        )
        return result


async def student_result(db, p, storage, entry, roster, cache, source_rows, config):
    result = StudentResult(
        participation_id=entry["participation_id"],
        roster_entry_id=entry["id"],
        student_id=entry["student_id"],
        student_name=entry["student_name"],
        student_code=entry["student_code"],
        exam_number=entry["exam_number"],
        class_id=entry["administrative_class_id"],
        class_name=entry["class_name"],
        status="identity_pending",
    )
    root = (
        (
            await db.execute(
                select(scripts.A).where(
                    scripts.A.c.exam_session_id == entry["exam_session_id"],
                    scripts.A.c.current_participation_id == entry["participation_id"],
                )
            )
        )
        .mappings()
        .first()
    )
    version = await g.row(db, scripts.V, root["current_version_id"]) if root else None
    paper_id = version["session_paper_id"] if version else entry["session_paper_id"]
    if paper_id is None:
        result.status = "variant_unverified"
        result.reasons = ["variant_unverified", "attendance_not_determined"]
        return result
    try:
        if paper_id not in cache:
            cache[paper_id] = await paper_questions(db, entry["exam_session_id"], paper_id)
        paper, pv, mappings, rules = cache[paper_id]
    except AccessError as error:
        if error.status_code in {413, 503}:
            raise
        result.status, result.reasons = "source_stale", ["paper_unavailable"]
        return result
    result.session_paper_id, result.paper_version_id = paper["id"], pv["id"]
    result.variant, result.required_count = paper["variant_code"], len(mappings)
    result.max_points = str(sum((m["max_score"] for m in mappings), Decimal("0.00")))
    result.questions = [
        QuestionResult(
            mapping_id=m["id"],
            question_id=m["legacy_question_id"],
            number=m["question_number"],
            max_points=str(m["max_score"]),
            status="not_scored",
        )
        for m in mappings
    ]
    if root is None:
        result.reasons = ["identity_not_confirmed", "attendance_not_determined"]
        return result
    result.script_id, result.script_version_id = root["id"], version["id"]
    if (
        version["roster_revision_id"] != roster["id"]
        or version["roster_entry_id"] != entry["id"]
        or await scripts.readiness(db, root, version) != "identity_confirmed"
    ):
        result.status, result.reasons = "source_stale", ["script_or_roster_changed"]
        return result
    job = (
        (
            await db.execute(
                select(g.parsing.J)
                .where(g.parsing.J.c.answer_script_version_id == version["id"])
                .order_by(g.parsing.J.c.created_at.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )
    parsing_not_complete = job is None or job["status"] != "succeeded"
    try:
        from huipi_cloud.modules.omr.service import result_questions

        objective = await result_questions(db, p, root, version, pv, source_rows)
        by_mapping = {q.mapping_id: q for q in result.questions}
        by_mapping.update(objective)
        if not parsing_not_complete and len(objective) < len(mappings):
            c = await g.context(db, p, job["id"])
            for row in c.source[2]:
                source_rows[row["id"]] = row
            # Canonical remains a checked source, even though only score summaries are returned.
            ca = c.canonical
            if ca["bucket"] != storage.bucket:
                raise AccessError("result_storage_unavailable", 503)
            checked = await g._read_checked_object(
                storage,
                ca["canonical_object_key"],
                expected_size=ca["canonical_size_bytes"],
                expected_sha256=ca["canonical_sha256"],
                maximum=config.canonical_max_document_bytes,
            )
            canonical = g.CanonicalDocument.model_validate_json(checked.data)
            if (
                canonical.document_id != ca["id"]
                or canonical.submission_id != c.link["submission_id"]
                or canonical.source_sha256 != ca["source_sha256"]
                or canonical.source_artifact_id != ca["parsed_artifact_id"]
            ):
                raise AccessError("result_source_changed", 409)
            workspaces = (
                (await db.execute(select(g.W).where(g.W.c.parsing_job_id == job["id"])))
                .mappings()
                .all()
            )
            workspaces_by_mapping = {w["paper_question_mapping_id"]: w for w in workspaces}
            for m in mappings:
                if m["id"] not in objective:
                    by_mapping[m["id"]] = await question_result(
                        db,
                        p,
                        storage,
                        c,
                        workspaces_by_mapping.get(m["id"]),
                        m,
                        rules[m["legacy_question_id"]],
                        config,
                    )
        result.questions = [by_mapping[m["id"]] for m in mappings]
    except AccessError as error:
        if error.status_code in {413, 503}:
            raise
        result.status, result.reasons = "source_stale", ["source_or_authority_changed"]
        return result
    result.reviewed_count = sum(q.status == "reviewed" for q in result.questions)
    result.subtotal = str(
        sum((Decimal(q.points) for q in result.questions if q.points is not None), Decimal("0.00"))
    )
    result.status = "complete" if result.reviewed_count == result.required_count else "incomplete"
    if result.reviewed_count == 0:
        result.subtotal = None
    if any(q.status == "source_stale" for q in result.questions):
        result.status = "source_stale"
    if result.status == "complete":
        result.total = result.subtotal
    result.reasons = sorted({q.status for q in result.questions if q.status != "reviewed"})
    if parsing_not_complete and any(m["id"] not in objective for m in mappings):
        result.reasons.append("parsing_not_complete")
    return result


async def preview(
    db,
    p,
    storage,
    sid,
    class_id=None,
    *,
    offset=0,
    limit=20,
    exporting=False,
    participation_id=None,
    config=settings,
):
    async with snapshot(db):
        classes = await access.authorize(db, p, sid, class_id, exporting=exporting)
        if exporting and class_id is None and classes != await access.authorize(db, p, sid):
            # Do not silently turn a visible multi-class preview into a partial export.
            raise AccessError("result_export_class_scope_incomplete", 403)
        # All existing roster/review/script writers serialize on this row. One response
        # cannot mix revisions while private artifacts are checked (bounded at 60 seconds).
        session = await scripts.exams.row(db, scripts.S, sid, lock=True)
        event = await g.row(db, scripts.EV, session["exam_event_id"])
        roster = (
            (
                await db.execute(
                    select(access.R).where(
                        access.R.c.exam_session_id == sid,
                        access.R.c.is_current,
                        access.R.c.status == "frozen",
                    )
                )
            )
            .mappings()
            .first()
        )
        query = (
            select(access.E).where(
                access.E.c.roster_revision_id == roster["id"],
                access.E.c.administrative_class_id.in_(classes),
            )
            if roster
            else None
        )
        total = await db.scalar(select(func.count()).select_from(query.subquery())) if roster else 0
        names = (
            (
                await db.execute(
                    select(access.E.c.administrative_class_id, access.E.c.class_name)
                    .where(
                        access.E.c.roster_revision_id == roster["id"],
                        access.E.c.administrative_class_id.in_(classes),
                    )
                    .distinct()
                )
            ).all()
            if roster
            else []
        )
        if class_id is not None and class_id not in {c for c, n in names}:
            raise AccessError(access.DENIED, 404)
        if exporting and total > MAX_EXPORT_STUDENTS:
            raise AccessError("result_export_student_limit", 413)
        if participation_id and roster:
            query = query.where(access.E.c.participation_id == participation_id)
        entries = (
            (
                await db.execute(
                    query.order_by(
                        access.E.c.class_name,
                        access.E.c.exam_number.nulls_last(),
                        access.E.c.student_code,
                        access.E.c.id,
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
            if roster
            else []
        )
        if participation_id and not entries:
            raise AccessError(access.DENIED, 404)
        cache, sources, students = {}, {}, []
        bounded = BudgetStorage(storage)
        for entry in entries:
            students.append(
                await student_result(db, p, bounded, entry, roster, cache, sources, config)
            )
            if sum(s.required_count or 0 for s in students) > MAX_CELLS:
                raise AccessError("result_preview_cell_limit", 413)
        if sources:
            await scripts.verify_sources(bounded, list(sources.values()))
        body = dict(
            session_id=sid,
            event_id=event["id"],
            event_title=event["title"],
            session_title=session["title"],
            roster_revision_id=roster["id"] if roster else None,
            generated_at=datetime.now(UTC),
            classes=[dict(class_id=c, class_name=n) for c, n in names],
            students=[s.model_dump(mode="json") for s in students],
            offset=offset,
            limit=limit,
            total_students=total,
            has_more=offset + len(entries) < total,
        )
        body["snapshot_sha256"] = "0" * 64
        result = Preview.model_validate(body)
        result.snapshot_sha256 = json_sha256(
            result.model_dump(mode="json", exclude={"snapshot_sha256"})
        )
        if len(result.model_dump_json().encode()) > 4 * 1024**2:
            raise AccessError("result_preview_size_limit", 413)
        return result
