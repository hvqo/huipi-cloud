"""Fixed scoped operations. Machine recognition is never itself a final score."""

import asyncio
import hashlib
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.modules.answer_scripts import service as scripts
from huipi_cloud.modules.canonical_documents.service import _read_checked_object
from huipi_cloud.modules.exam_grading import service as grading
from huipi_cloud.modules.exam_parsing import service as parsing
from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.papers import service as papers
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.resource_security.models import SecurityScope
from huipi_cloud.modules.scans import service as scans

from . import engine
from .models import OmrDecision, OmrOperation, OmrRecognition, OmrTemplate, OmrTemplateQuestion
from .schemas import MachineQuestion

T, TQ, REC, DEC, OPS = (
    model.__table__
    for model in (OmrTemplate, OmrTemplateQuestion, OmrRecognition, OmrDecision, OmrOperation)
)
MAX_PARALLEL = asyncio.Semaphore(2)
DENIED = "resource_not_found_or_forbidden"


async def bounded_cpu(fn, *args):
    try:
        async with asyncio.timeout(2):
            await MAX_PARALLEL.acquire()
    except TimeoutError:
        raise AccessError("omr_busy", 429) from None
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise
    finally:
        MAX_PARALLEL.release()


@dataclass
class Operation:
    replay: dict | None
    result: dict | None = None


@asynccontextmanager
async def operation(db, p, request_id, action, payload):
    digest = scripts.exams.digest(dict(action=action, payload=payload))
    committing = False
    try:
        await db.begin()
        await db.execute(select(func.set_config("statement_timeout", "8000", True)))
        await identity.refresh_principal(db, p, lock=True)
        key = int.from_bytes(
            hashlib.sha256(f"omr:{p.user_id}:{request_id}".encode()).digest()[:8],
            signed=True,
        )
        await db.execute(select(func.pg_advisory_xact_lock(key)))
        previous = (
            (
                await db.execute(
                    select(OPS).where(
                        OPS.c.actor_user_id == p.user_id, OPS.c.request_id == request_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if previous and previous["payload_sha256"] != digest:
            raise AccessError("omr_request_conflict", 409)
        op = Operation(dict(previous["result"]) if previous else None)
        yield op
        if op.replay is None:
            if op.result is None:
                raise RuntimeError("OMR operation result missing")
            await db.execute(
                insert(OPS).values(
                    id=uuid4(),
                    actor_user_id=p.user_id,
                    request_id=request_id,
                    action=action,
                    payload_sha256=digest,
                    result=op.result,
                )
            )
        committing = True
        await db.commit()
    except BaseException as error:
        try:
            await db.rollback()
        except (SQLAlchemyError, OSError, TimeoutError):
            pass
        if isinstance(error, (SQLAlchemyError, OSError, TimeoutError)):
            code = getattr(getattr(error, "orig", None), "sqlstate", None)
            if not committing and code in {"23505", "23514", "23503", "40001", "40P01"}:
                raise AccessError("omr_write_conflict", 409) from None
            raise AccessError(
                "omr_commit_unknown" if committing else "omr_unavailable", 503
            ) from None
        raise


async def paper_authority(db, p, paper_id, *, write=False):
    scope_id = await db.scalar(
        select(SecurityScope.id).where(
            SecurityScope.kind == "paper_version", SecurityScope.paper_version_id == paper_id
        )
    )
    if scope_id is None:
        raise AccessError(DENIED, 404)
    actions = {"paper.read", "paper.edit"} if write else {"paper.read"}
    await security._authority(db, p, actions, await security.scope(db, scope_id))


async def http_authorize(db, p, params, kind):
    from uuid import UUID

    if kind.startswith("paper_"):
        return await paper_authority(
            db, p, UUID(str(params["paper_id"])), write=kind.endswith("write")
        )
    if kind == "template":
        template = await scripts.exams.row(db, T, UUID(str(params["template_id"])))
        return await paper_authority(db, p, template["paper_version_id"])
    if kind.startswith("recognition_"):
        rec = await scripts.exams.row(db, REC, UUID(str(params["recognition_id"])))
        script_id = rec["script_id"]
    else:
        script_id = UUID(str(params["script_id"]))
    root = await scripts.exams.row(db, scripts.A, script_id)
    await session_authority(db, p, root["exam_session_id"], write=kind.endswith("write"))


async def checked_paper(db, paper_id, *, lock=False):
    paper = await papers.row(db, paper_id, lock=lock)
    if paper["status"] != "locked":
        raise AccessError("omr_paper_not_locked", 409)
    await papers.ensure_budget(db, paper["canonical_assignment_id"])
    values = await papers.digests(db, paper["canonical_assignment_id"])
    if any(values[k] != paper[k] for k in values) or (
        await papers.mapping_digest(db, paper_id) != paper["mapping_digest"]
    ):
        raise AccessError("omr_paper_changed", 409)
    return paper


async def available_questions(db, paper):
    rubric_totals = (
        select(grading.RU.c.question_id, func.sum(grading.RU.c.points).label("points"))
        .group_by(grading.RU.c.question_id)
        .subquery()
    )
    rows = (
        (
            await db.execute(
                select(
                    grading.QM.c.id.label("mapping_id"),
                    grading.QM.c.printed_label,
                    grading.Q.c.max_score,
                    grading.AK.c.answer_content,
                    grading.AK.c.source,
                    rubric_totals.c.points.label("rubric_points"),
                )
                .join(grading.Q, grading.Q.c.id == grading.QM.c.legacy_question_id)
                .join(grading.AK, grading.AK.c.question_id == grading.Q.c.id)
                .join(rubric_totals, rubric_totals.c.question_id == grading.Q.c.id)
                .where(
                    grading.QM.c.paper_version_id == paper["id"],
                    grading.Q.c.assignment_id == paper["canonical_assignment_id"],
                    grading.Q.c.question_type == "single_choice",
                )
                .order_by(grading.QM.c.sort_order)
                .limit(201)
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > 200:
        raise AccessError("omr_question_limit", 413)
    values = []
    for row in rows:
        label, answer = row["printed_label"], row["answer_content"].strip()
        if (
            row["source"] != "teacher"
            or answer not in engine.OPTIONS
            or len(answer) != 1
            or re.fullmatch(r"[A-Za-z0-9._()\-]{1,12}", label) is None
        ):
            continue
        if row["rubric_points"] != row["max_score"]:
            continue
        values.append(
            dict(
                mapping_id=row["mapping_id"],
                printed_label=label,
                correct_choice=answer,
                max_points=str(row["max_score"]),
            )
        )
    return values


async def selected_questions(db, template, paper):
    available = {q["mapping_id"]: q for q in await available_questions(db, paper)}
    rows = (
        (
            await db.execute(
                select(TQ).where(TQ.c.template_id == template["id"]).order_by(TQ.c.ordinal)
            )
        )
        .mappings()
        .all()
    )
    if (
        not rows
        or len(rows) > engine.MAX_QUESTIONS
        or [r["ordinal"] for r in rows] != list(range(len(rows)))
    ):
        raise AccessError("omr_template_invalid", 409)
    result = []
    for r in rows:
        value = available.get(r["mapping_id"])
        if value is None or value["printed_label"] != r["printed_label"]:
            raise AccessError("omr_template_changed", 409)
        result.append(value)
    if template["engine_version"] != engine.ENGINE_VERSION or (
        template["content_sha256"] != paper["content_sha256"]
        or template["mapping_digest"] != paper["mapping_digest"]
    ):
        raise AccessError("omr_template_changed", 409)
    return result


async def template_view(db, p, paper_id):
    async with scans.transaction(db):
        await paper_authority(db, p, paper_id)
        paper = await checked_paper(db, paper_id)
        template = (
            (await db.execute(select(T).where(T.c.paper_version_id == paper_id))).mappings().first()
        )
        questions = (
            await selected_questions(db, template, paper)
            if template
            else await available_questions(db, paper)
        )
        return dict(
            template_id=template["id"] if template else None,
            paper_version_id=paper_id,
            title=paper["title"],
            questions=questions,
        )


async def create_template(db, p, paper_id, request):
    payload = dict(paper_id=str(paper_id), body=request.model_dump(mode="json"))
    async with operation(db, p, request.request_id, "template", payload) as op:
        await paper_authority(db, p, paper_id, write=True)
        paper = await checked_paper(db, paper_id, lock=True)
        if op.replay is not None:
            return op.replay
        if await db.scalar(select(T.c.id).where(T.c.paper_version_id == paper_id)):
            raise AccessError("omr_template_already_exists", 409)
        available = await available_questions(db, paper)
        selected = set(request.mapping_ids)
        if len(selected) != len(request.mapping_ids):
            raise AccessError("omr_duplicate_question", 422)
        questions = [q for q in available if q["mapping_id"] in selected]
        if len(questions) != len(selected):
            raise AccessError("omr_question_not_supported", 422)
        identifier = uuid4()
        await db.execute(
            insert(T).values(
                id=identifier,
                paper_version_id=paper_id,
                engine_version=engine.ENGINE_VERSION,
                content_sha256=paper["content_sha256"],
                mapping_digest=paper["mapping_digest"],
                created_by_user_id=p.user_id,
            )
        )
        await db.execute(
            insert(TQ),
            [
                dict(
                    template_id=identifier,
                    ordinal=i,
                    paper_version_id=paper_id,
                    mapping_id=q["mapping_id"],
                    printed_label=q["printed_label"],
                )
                for i, q in enumerate(questions)
            ],
        )
        op.result = dict(id=str(identifier))
        return op.result


async def template_pdf(db, p, template_id):
    async with scans.transaction(db):
        template = await scripts.exams.row(db, T, template_id)
        await paper_authority(db, p, template["paper_version_id"])
        paper = await checked_paper(db, template["paper_version_id"])
        questions = await selected_questions(db, template, paper)
        return await bounded_cpu(
            engine.render_pdf, str(template_id), [q["printed_label"] for q in questions]
        )


async def session_authority(db, p, sid, *, write=False):
    actions = ["exam.mark.read", "exam.script.read", "exam.identity.verify"]
    if write:
        actions.append("exam.score.review")
    await scripts.authorize(db, p, sid, *actions)
    await scripts.authorize(db, p, sid, "exam.roster.read")


async def source_context(db, p, script_id, *, write=False, version_id=None):
    root = await scripts.exams.row(db, scripts.A, script_id)
    await session_authority(db, p, root["exam_session_id"], write=write)
    return await parsing.context(db, p, script_id, action="exam.mark.read", version_id=version_id)


async def source_with_template(db, p, script_id, template_id, *, write=False, version_id=None):
    root, version, rows, sp, pv = await source_context(
        db, p, script_id, write=write, version_id=version_id
    )
    template = await scripts.exams.row(db, T, template_id)
    if template["paper_version_id"] != pv["id"]:
        raise AccessError("omr_paper_mismatch", 409)
    paper = await checked_paper(db, pv["id"])
    questions = await selected_questions(db, template, paper)
    return root, version, rows, template, questions


async def recognize(db, p, storage, script_id, request):
    async with scans.transaction(db):
        root, version, rows, template, questions = await source_with_template(
            db, p, script_id, request.template_id, write=True
        )
        if root["revision"] != request.expected_revision:
            raise AccessError("script_revision_conflict", 409)
        page = next((r for r in rows if r["id"] == request.scan_page_id), None)
        if page is None:
            raise AccessError(DENIED, 404)
        source_id = version["id"]
    await scripts.verify_sources(storage, rows)
    data = await _read_checked_object(
        storage,
        page["object_key"],
        expected_size=page["size_bytes"],
        expected_sha256=page["sha256"],
        maximum=2 * 1024 * 1024,
    )
    try:
        results = await bounded_cpu(
            engine.recognize,
            data.data,
            str(template["id"]),
            [q["printed_label"] for q in questions],
        )
    except engine.SheetError as error:
        raise AccessError(f"omr_{error}", 422) from None
    for result, question in zip(results, questions, strict=True):
        result["mapping_id"] = str(question["mapping_id"])
    payload = dict(script_id=str(script_id), body=request.model_dump(mode="json"))
    async with operation(db, p, request.request_id, "recognize", payload) as op:
        root, version, current, template, questions = await source_with_template(
            db, p, script_id, request.template_id, write=True, version_id=source_id
        )
        if root["revision"] != request.expected_revision:
            raise AccessError("script_revision_conflict", 409)
        selected = next((r for r in current if r["id"] == request.scan_page_id), None)
        if selected is None or selected["sha256"] != page["sha256"]:
            raise AccessError("omr_source_changed", 409)
        if op.replay is not None:
            return op.replay
        existing = await db.scalar(
            select(REC.c.id).where(
                REC.c.script_version_id == source_id, REC.c.template_id == template["id"]
            )
        )
        if existing:
            raise AccessError("omr_recognition_already_exists", 409)
        identifier = uuid4()
        await db.execute(
            insert(REC).values(
                id=identifier,
                template_id=template["id"],
                paper_version_id=template["paper_version_id"],
                script_id=script_id,
                script_version_id=source_id,
                exam_session_id=root["exam_session_id"],
                scan_page_id=selected["id"],
                manifest_sha256=version["manifest_sha256"],
                page_sha256=selected["sha256"],
                results=results,
                created_by_user_id=p.user_id,
            )
        )
        op.result = dict(id=str(identifier))
        return op.result


async def recognition_context(db, p, recognition_id, *, write=False):
    recognition = await scripts.exams.row(db, REC, recognition_id)
    root, version, rows, template, questions = await source_with_template(
        db,
        p,
        recognition["script_id"],
        recognition["template_id"],
        write=write,
        version_id=recognition["script_version_id"],
    )
    if write:
        creator = await parsing.actor(db, recognition["created_by_user_id"])
        await source_context(
            db, creator, root["id"], write=True, version_id=recognition["script_version_id"]
        )
    page = next((r for r in rows if r["id"] == recognition["scan_page_id"]), None)
    if (
        page is None
        or page["sha256"] != recognition["page_sha256"]
        or version["manifest_sha256"] != recognition["manifest_sha256"]
    ):
        raise AccessError("omr_source_changed", 409)
    try:
        raw = TypeAdapter(list[MachineQuestion]).validate_python(recognition["results"])
    except ValidationError:
        raise AccessError("omr_result_invalid", 409) from None
    if len(raw) != len(questions) or any(
        r.mapping_id != q["mapping_id"] or r.label != q["printed_label"]
        for r, q in zip(raw, questions, strict=True)
    ):
        raise AccessError("omr_result_invalid", 409)
    return recognition, rows, page, questions


async def decisions(db, recognition_id):
    rows = (
        (
            await db.execute(
                select(DEC)
                .where(DEC.c.recognition_id == recognition_id)
                .ext(distinct_on(DEC.c.mapping_id))
                .order_by(DEC.c.mapping_id, DEC.c.revision.desc())
                .limit(engine.MAX_QUESTIONS)
            )
        )
        .mappings()
        .all()
    )
    result = {}
    for r in rows:
        result.setdefault(r["mapping_id"], r)
    return result


async def inspect(db, p, recognition_id):
    async with scans.transaction(db):
        rec, rows, page, questions = await recognition_context(db, p, recognition_id)
        current = await decisions(db, recognition_id)
        values = []
        for raw, q in zip(rec["results"], questions, strict=True):
            d = current.get(q["mapping_id"])
            suggestion = (
                (q["max_points"] if raw["choice"] == q["correct_choice"] else "0.00")
                if raw["status"] == "single"
                else None
            )
            values.append(
                dict(
                    **raw,
                    correct_choice=q["correct_choice"],
                    suggested_points=suggestion,
                    confirmed_choice=d["choice"] if d else None,
                    confirmed_points=str(d["points"]) if d else None,
                    revision=d["revision"] if d else 0,
                )
            )
        return dict(
            recognition_id=rec["id"],
            template_id=rec["template_id"],
            script_id=rec["script_id"],
            script_version_id=rec["script_version_id"],
            scan_page_id=rec["scan_page_id"],
            questions=values,
        )


async def current(db, p, script_id):
    async with scans.transaction(db):
        root, version, _, _, paper = await source_context(db, p, script_id)
        template_id = await db.scalar(select(T.c.id).where(T.c.paper_version_id == paper["id"]))
        result = await db.scalar(select(REC.c.id).where(REC.c.script_version_id == version["id"]))
        return dict(
            id=result,
            template_id=template_id,
            paper_version_id=paper["id"],
            script_version_id=version["id"],
            revision=root["revision"],
        )


async def image(db, p, storage, recognition_id, *, overlay=False):
    async with scans.transaction(db):
        rec, _, page, questions = await recognition_context(db, p, recognition_id)
        value = await _read_checked_object(
            storage,
            page["object_key"],
            expected_size=page["size_bytes"],
            expected_sha256=page["sha256"],
            maximum=2 * 1024 * 1024,
        )
        if not overlay:
            return bytes(value.data)
        try:
            return await bounded_cpu(
                engine.evidence_overlay,
                bytes(value.data),
                str(rec["template_id"]),
                [q["printed_label"] for q in questions],
            )
        except engine.SheetError as error:
            raise AccessError(f"omr_{error}", 422) from None


async def confirm(db, p, storage, recognition_id, request):
    async with scans.transaction(db):
        rec, rows, _, _ = await recognition_context(db, p, recognition_id, write=True)
    await scripts.verify_sources(storage, rows)
    payload = dict(recognition_id=str(recognition_id), body=request.model_dump(mode="json"))
    async with operation(db, p, request.request_id, "confirm", payload) as op:
        rec, _, _, questions = await recognition_context(db, p, recognition_id, write=True)
        if op.replay is not None:
            return op.replay
        by_id = {q["mapping_id"]: q for q in questions}
        latest = await decisions(db, recognition_id)
        if len({d.mapping_id for d in request.decisions}) != len(request.decisions):
            raise AccessError("omr_duplicate_question", 422)
        for d in request.decisions:
            question = by_id.get(d.mapping_id)
            previous = latest.get(d.mapping_id)
            if question is None:
                raise AccessError(DENIED, 404)
            if (previous["revision"] if previous else 0) != d.expected_revision:
                raise AccessError("omr_revision_conflict", 409)
            points = (
                Decimal(question["max_points"])
                if d.choice == question["correct_choice"]
                else Decimal("0.00")
            )
            await db.execute(
                insert(DEC).values(
                    id=uuid4(),
                    recognition_id=rec["id"],
                    template_id=rec["template_id"],
                    mapping_id=d.mapping_id,
                    revision=d.expected_revision + 1,
                    choice=d.choice,
                    points=points,
                    reason=d.reason.strip(),
                    actor_user_id=p.user_id,
                )
            )
        op.result = dict(id=str(recognition_id))
        return op.result


async def result_questions(db, p, root, version, paper, source_rows):
    """Only explicitly confirmed choices can contribute; revalidate all source authority."""
    from huipi_cloud.modules.exam_results.schemas import QuestionResult

    template = (
        (await db.execute(select(T).where(T.c.paper_version_id == paper["id"]))).mappings().first()
    )
    if not template:
        return {}
    questions = await selected_questions(db, template, paper)
    mapping_rows = (
        (
            await db.execute(
                select(
                    grading.QM.c.id, grading.QM.c.legacy_question_id, grading.Q.c.question_number
                )
                .join(grading.Q, grading.Q.c.id == grading.QM.c.legacy_question_id)
                .where(grading.QM.c.id.in_([q["mapping_id"] for q in questions]))
            )
        )
        .mappings()
        .all()
    )
    mappings = {r["id"]: r for r in mapping_rows}
    output = {
        q["mapping_id"]: QuestionResult(
            mapping_id=q["mapping_id"],
            question_id=mappings[q["mapping_id"]]["legacy_question_id"],
            number=mappings[q["mapping_id"]]["question_number"],
            max_points=q["max_points"],
            status="not_scored",
            evidence_source="omr",
        )
        for q in questions
    }
    rec = (
        (
            await db.execute(
                select(REC).where(
                    REC.c.script_version_id == version["id"], REC.c.template_id == template["id"]
                )
            )
        )
        .mappings()
        .first()
    )
    if rec is None:
        return output
    rec, rows, _, _ = await recognition_context(db, p, rec["id"])
    # The actor who created the recognition must still be permitted; current authority
    # and immutable provenance are not transferable to another viewer.
    creator = await parsing.actor(db, rec["created_by_user_id"])
    await source_context(db, creator, root["id"], write=True, version_id=version["id"])
    for r in rows:
        source_rows[r["id"]] = r
    current = await decisions(db, rec["id"])
    checked_actors = {rec["created_by_user_id"]}
    for q in questions:
        decision = current.get(q["mapping_id"])
        if not decision:
            continue
        if decision["actor_user_id"] not in checked_actors:
            actor = await parsing.actor(db, decision["actor_user_id"])
            await source_context(db, actor, root["id"], write=True, version_id=version["id"])
            checked_actors.add(decision["actor_user_id"])
        expected = (
            Decimal(q["max_points"])
            if decision["choice"] == q["correct_choice"]
            else Decimal("0.00")
        )
        if (
            decision["choice"] not in {"A", "B", "C", "D", "blank"}
            or decision["points"] != expected
        ):
            raise AccessError("omr_decision_invalid", 409)
        output[q["mapping_id"]].status = "reviewed"
        output[q["mapping_id"]].points = str(expected)
        output[q["mapping_id"]].omr_decision_id = decision["id"]
        output[q["mapping_id"]].review_status = "omr_confirmed"
    return output
