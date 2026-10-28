"""Authenticated attendance and immutable, provenance-checked final score versions."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.modules.exam_grading import service as g
from huipi_cloud.modules.grading.protocol import json_sha256
from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.scans import service as scans

from . import access
from . import service as preview
from .models import (
    ExamAttendanceDecision,
    ExamFinalOperation,
    ExamFinalQuestion,
    ExamFinalStudent,
    ExamFinalVersion,
)

A, OPS, Q, F, V = (
    m.__table__
    for m in (
        ExamAttendanceDecision,
        ExamFinalOperation,
        ExamFinalQuestion,
        ExamFinalStudent,
        ExamFinalVersion,
    )
)
DENIED = "resource_not_found_or_forbidden"
LIMIT = 100


@dataclass
class Operation:
    replay: dict | None = None
    result: dict | None = None


@asynccontextmanager
async def operation(db, p, sid, request_id, action, payload):
    digest = json_sha256(dict(session=str(sid), action=action, body=payload))
    committing = False
    try:
        async with asyncio.timeout(60):
            await db.begin()
            await db.execute(text("SET LOCAL statement_timeout='8000ms'"))
            await db.execute(text("SET LOCAL lock_timeout='5000ms'"))
            await identity.refresh_principal(db, p, lock=True)
            await db.execute(
                select(
                    func.pg_advisory_xact_lock(
                        int.from_bytes(
                            hashlib.sha256(f"final:{p.user_id}:{request_id}".encode()).digest()[:8],
                            signed=True,
                        )
                    )
                )
            )
            old = (
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
            if old and old["payload_sha256"] != digest:
                raise AccessError("final_request_conflict", 409)
            op = Operation(dict(old["result"]) if old else None)
            yield op
            if op.replay is None:
                if op.result is None:
                    raise RuntimeError("missing final operation result")
                await db.execute(
                    insert(OPS).values(
                        id=uuid4(),
                        exam_session_id=sid,
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
            state = getattr(getattr(error, "orig", None), "sqlstate", None)
            if not committing and state in {"23505", "23503", "23514", "40001", "40P01"}:
                raise AccessError("final_write_conflict", 409) from None
            raise AccessError(
                "final_commit_outcome_unknown" if committing else "final_unavailable", 503
            ) from None
        raise


async def roster(db, sid):
    value = (
        (
            await db.execute(
                select(access.R).where(
                    access.R.c.exam_session_id == sid,
                    access.R.c.status == "frozen",
                    access.R.c.is_current,
                )
            )
        )
        .mappings()
        .first()
    )
    if value is None:
        raise AccessError("final_roster_unavailable", 409)
    return value


async def authorized_all(db, p, sid, action):
    classes = await access.authorize(db, p, sid, extra_actions={action})
    await scans.writable_session(db, sid)
    current = await roster(db, sid)
    actual = set(
        (
            await db.scalars(
                select(access.E.c.administrative_class_id)
                .where(access.E.c.roster_revision_id == current["id"])
                .distinct()
            )
        ).all()
    )
    if not actual or not actual <= classes:
        raise AccessError(DENIED, 404)
    return current


async def current_attendance(db, sid, participation_id):
    return (
        (
            await db.execute(
                select(A)
                .where(A.c.exam_session_id == sid, A.c.participation_id == participation_id)
                .order_by(A.c.revision.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )


async def record_attendance(db, p, sid, body):
    async with operation(
        db, p, sid, body.request_id, "attendance", body.model_dump(mode="json")
    ) as op:
        current = await authorized_all(db, p, sid, "exam.result.manage")
        entry = (
            (
                await db.execute(
                    select(access.E).where(
                        access.E.c.roster_revision_id == current["id"],
                        access.E.c.participation_id == body.participation_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if entry is None:
            raise AccessError(DENIED, 404)
        previous = await current_attendance(db, sid, body.participation_id)
        if op.replay is not None:
            if (
                not previous
                or str(previous["id"]) != op.replay["attendance_id"]
                or previous["roster_entry_id"] != entry["id"]
            ):
                raise AccessError("final_attendance_changed", 409)
            return op.replay
        if (previous["revision"] if previous else 0) != body.expected_revision:
            raise AccessError("final_attendance_revision_conflict", 409)
        if body.status == "absent":
            script = await db.scalar(
                select(preview.scripts.A.c.id).where(
                    preview.scripts.A.c.exam_session_id == sid,
                    preview.scripts.A.c.current_participation_id == body.participation_id,
                )
            )
            if script is not None:
                raise AccessError("final_absent_has_confirmed_script", 409)
        identifier = uuid4()
        await db.execute(
            insert(A).values(
                id=identifier,
                exam_session_id=sid,
                roster_revision_id=current["id"],
                roster_entry_id=entry["id"],
                participation_id=body.participation_id,
                revision=body.expected_revision + 1,
                status=body.status,
                reason=body.reason,
                actor_user_id=p.user_id,
            )
        )
        op.result = {"attendance_id": str(identifier), "revision": body.expected_revision + 1}
        return op.result


async def attendance_list(db, p, sid, class_id=None):
    async with preview.snapshot(db):
        classes = await access.authorize(db, p, sid, class_id)
        current = await roster(db, sid)
        rows = (
            (
                await db.execute(
                    select(access.E)
                    .where(
                        access.E.c.roster_revision_id == current["id"],
                        access.E.c.administrative_class_id.in_(classes),
                    )
                    .order_by(access.E.c.class_name, access.E.c.student_code)
                    .limit(LIMIT + 1)
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > LIMIT:
            raise AccessError("final_roster_limit", 413)
        items = []
        for entry in rows:
            a = await current_attendance(db, sid, entry["participation_id"])
            valid = a is not None and a["roster_entry_id"] == entry["id"]
            items.append(
                dict(
                    participation_id=str(entry["participation_id"]),
                    student_name=entry["student_name"],
                    student_code=entry["student_code"],
                    exam_number=entry["exam_number"],
                    class_id=str(entry["administrative_class_id"]),
                    class_name=entry["class_name"],
                    status=a["status"] if valid else "unverified",
                    revision=a["revision"] if a else 0,
                    reason=a["reason"] if valid else None,
                )
            )
        return {"roster_revision_id": str(current["id"]), "items": items}


async def live_snapshot(db, p, storage, sid, roster_row, config):
    entries = (
        (
            await db.execute(
                select(access.E)
                .where(access.E.c.roster_revision_id == roster_row["id"])
                .order_by(access.E.c.id)
                .limit(LIMIT + 1)
            )
        )
        .mappings()
        .all()
    )
    if not entries:
        raise AccessError("final_roster_empty", 409)
    if len(entries) > LIMIT:
        raise AccessError("final_roster_limit", 413)
    cache, sources, rows = {}, {}, []
    bounded = preview.BudgetStorage(storage)
    cells = 0
    for entry in entries:
        a = await current_attendance(db, sid, entry["participation_id"])
        if not a or a["roster_entry_id"] != entry["id"] or a["status"] == "unverified":
            raise AccessError("final_attendance_unverified", 409)
        if a["status"] == "absent":
            root = await db.scalar(
                select(preview.scripts.A.c.id).where(
                    preview.scripts.A.c.exam_session_id == sid,
                    preview.scripts.A.c.current_participation_id == entry["participation_id"],
                )
            )
            if root:
                raise AccessError("final_absent_has_confirmed_script", 409)
            student = None
        else:
            student = await preview.student_result(
                db, p, bounded, entry, roster_row, cache, sources, config
            )
            if student.status != "complete" or student.total is None:
                raise AccessError("final_results_incomplete", 409)
            cells += student.required_count or 0
            if cells > preview.MAX_CELLS:
                raise AccessError("final_result_cell_limit", 413)
        rows.append((entry, a, student))
    if sources:
        await preview.scripts.verify_sources(bounded, list(sources.values()))
    return rows


def snapshot_data(roster_id, rows):
    values = []
    for entry, a, student in rows:
        values.append(
            dict(
                roster_entry_id=str(entry["id"]),
                participation_id=str(entry["participation_id"]),
                class_id=str(entry["administrative_class_id"]),
                attendance_id=str(a["id"]),
                attendance=a["status"],
                script_version_id=str(student.script_version_id) if student else None,
                session_paper_id=str(student.session_paper_id) if student else None,
                paper_version_id=str(student.paper_version_id) if student else None,
                total=student.total if student else None,
                max_points=student.max_points if student else None,
                questions=[
                    dict(
                        mapping_id=str(q.mapping_id),
                        question_number=q.number,
                        points=q.points,
                        max_points=q.max_points,
                        review_id=str(q.review_id) if q.review_id else None,
                        omr_decision_id=str(q.omr_decision_id) if q.omr_decision_id else None,
                    )
                    for q in student.questions
                ]
                if student
                else [],
            )
        )
    return dict(roster_revision_id=str(roster_id), students=values)


async def prepare(db, p, storage, sid, body, config):
    async with operation(
        db, p, sid, body.request_id, "prepare", body.model_dump(mode="json")
    ) as op:
        current = await authorized_all(db, p, sid, "exam.result.manage")
        if op.replay is not None:
            return op.replay
        latest = (
            (
                await db.execute(
                    select(V)
                    .where(V.c.exam_session_id == sid, V.c.status == "published")
                    .order_by(V.c.version_no.desc())
                    .limit(1)
                )
            )
            .mappings()
            .first()
        )
        if (latest["id"] if latest else None) != body.previous_version_id:
            raise AccessError("final_previous_version_changed", 409)
        rows = await live_snapshot(db, p, storage, sid, current, config)
        data = snapshot_data(current["id"], rows)
        digest = json_sha256(data)
        if latest and latest["snapshot_sha256"] == digest:
            raise AccessError("final_no_changes", 409)
        number = (
            await db.scalar(
                select(func.coalesce(func.max(V.c.version_no), 0)).where(V.c.exam_session_id == sid)
            )
        ) + 1
        vid = uuid4()
        await db.execute(
            insert(V).values(
                id=vid,
                exam_session_id=sid,
                roster_revision_id=current["id"],
                version_no=number,
                supersedes_id=latest["id"] if latest else None,
                snapshot_sha256=digest,
                reason=body.reason,
                created_by_user_id=p.user_id,
            )
        )
        for entry, attendance, student in rows:
            item = uuid4()
            await db.execute(
                insert(F).values(
                    id=item,
                    version_id=vid,
                    participation_id=entry["participation_id"],
                    roster_entry_id=entry["id"],
                    attendance_id=attendance["id"],
                    class_id=entry["administrative_class_id"],
                    attendance=attendance["status"],
                    script_version_id=student.script_version_id if student else None,
                    session_paper_id=student.session_paper_id if student else None,
                    paper_version_id=student.paper_version_id if student else None,
                    total=Decimal(student.total) if student else None,
                    max_points=Decimal(student.max_points) if student else None,
                )
            )
            if student:
                for question in student.questions:
                    await db.execute(
                        insert(Q).values(
                            id=uuid4(),
                            student_result_id=item,
                            mapping_id=question.mapping_id,
                            review_id=question.review_id,
                            omr_decision_id=question.omr_decision_id,
                            question_number=question.number,
                            points=Decimal(question.points),
                            max_points=Decimal(question.max_points),
                        )
                    )
        op.result = {
            "version_id": str(vid),
            "version_no": number,
            "revision": 1,
            "status": "pending",
        }
        return op.result


async def transition(db, p, storage, sid, vid, body, config):
    action = {
        "approve": "exam.result.approve",
        "publish": "exam.result.publish",
        "cancel": "exam.result.manage",
    }[body.action]
    async with operation(
        db,
        p,
        sid,
        body.request_id,
        body.action,
        {"version_id": str(vid), "body": body.model_dump(mode="json")},
    ) as op:
        current = await authorized_all(db, p, sid, action)
        version = (
            (
                await db.execute(
                    select(V).where(V.c.id == vid, V.c.exam_session_id == sid).with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if version is None:
            raise AccessError(DENIED, 404)
        if op.replay is not None:
            return op.replay
        if version["revision"] != body.expected_revision:
            raise AccessError("final_revision_conflict", 409)
        allowed = {"approve": ("pending", "approved"), "publish": ("approved", "published")}
        if body.action == "cancel":
            if version["status"] not in {"pending", "approved"}:
                raise AccessError("final_transition_invalid", 409)
            target = "cancelled"
        else:
            before, target = allowed[body.action]
            if version["status"] != before:
                raise AccessError("final_transition_invalid", 409)
            rows = await live_snapshot(db, p, storage, sid, current, config)
            if (
                version["roster_revision_id"] != current["id"]
                or json_sha256(snapshot_data(current["id"], rows)) != version["snapshot_sha256"]
            ):
                raise AccessError("final_source_changed_reprepare", 409)
        values = dict(status=target, revision=version["revision"] + 1)
        if body.action == "approve":
            values.update(approved_by_user_id=p.user_id, approved_at=datetime.now(UTC))
        if body.action == "publish":
            values.update(published_by_user_id=p.user_id, published_at=datetime.now(UTC))
        await db.execute(update(V).where(V.c.id == vid).values(**values))
        op.result = {"version_id": str(vid), "revision": values["revision"], "status": target}
        return op.result


async def versions(db, p, sid):
    async with preview.snapshot(db):
        await access.authorize(db, p, sid)
        try:
            managed = await access.authorize(db, p, sid, extra_actions={"exam.result.manage"})
        except AccessError as error:
            if error.status_code != 404:
                raise
            managed = set()
        rows = (
            (
                await db.execute(
                    select(V)
                    .where(V.c.exam_session_id == sid)
                    .order_by(V.c.version_no.desc())
                    .limit(101)
                )
            )
            .mappings()
            .all()
        )
        items = []
        for row in rows[:100]:
            required_classes = set(
                (
                    await db.scalars(
                        select(access.E.c.administrative_class_id)
                        .where(access.E.c.roster_revision_id == row["roster_revision_id"])
                        .distinct()
                    )
                ).all()
            )
            items.append(
                dict(
                    id=str(row["id"]),
                    version_no=row["version_no"],
                    revision=row["revision"],
                    status=row["status"],
                    reason=row["reason"]
                    if required_classes and required_classes <= managed
                    else "仅考务负责人可见",
                    snapshot_sha256=row["snapshot_sha256"],
                    supersedes_id=str(row["supersedes_id"]) if row["supersedes_id"] else None,
                    created_at=row["created_at"].isoformat(),
                    approved_at=row["approved_at"].isoformat() if row["approved_at"] else None,
                    published_at=row["published_at"].isoformat() if row["published_at"] else None,
                )
            )
        return {"items": items, "has_more": len(rows) > 100}


async def final_view(db, p, sid, vid, class_id=None, *, exporting=False):
    from .schemas import FormalPreview, QuestionResult, StudentResult

    async with preview.snapshot(db):
        classes = await access.authorize(db, p, sid, class_id, exporting=exporting)
        if exporting and class_id is None and classes != await access.authorize(db, p, sid):
            raise AccessError("result_export_class_scope_incomplete", 403)
        version = (
            (await db.execute(select(V).where(V.c.id == vid, V.c.exam_session_id == sid)))
            .mappings()
            .first()
        )
        if version is None:
            raise AccessError(DENIED, 404)
        if version["status"] != "published":
            if exporting or version["status"] not in {"pending", "approved"}:
                raise AccessError(DENIED, 404)
            await authorized_all(db, p, sid, "exam.result.approve")
        session = await g.row(db, preview.scripts.S, sid)
        event = await g.row(db, preview.scripts.EV, session["exam_event_id"])
        entries = (
            (
                await db.execute(
                    select(F, access.E)
                    .join(access.E, access.E.c.id == F.c.roster_entry_id)
                    .where(F.c.version_id == vid, F.c.class_id.in_(classes))
                    .order_by(access.E.c.class_name, access.E.c.student_code, F.c.id)
                    .limit(LIMIT + 1)
                )
            )
            .mappings()
            .all()
        )
        if len(entries) > LIMIT:
            raise AccessError("final_roster_limit", 413)
        if class_id and not entries:
            raise AccessError(DENIED, 404)
        students, names = [], {}
        for row in entries:
            names[row["class_id"]] = row["class_name"]
            question_rows = (
                (
                    await db.execute(
                        select(Q, g.QM.c.legacy_question_id)
                        .join(g.QM, g.QM.c.id == Q.c.mapping_id)
                        .where(Q.c.student_result_id == row["id"])
                        .order_by(Q.c.question_number, Q.c.id)
                    )
                )
                .mappings()
                .all()
            )
            paper = (
                await g.row(db, preview.scripts.SP, row["session_paper_id"])
                if row["session_paper_id"]
                else None
            )
            script = (
                await g.row(db, preview.scripts.V, row["script_version_id"])
                if row["script_version_id"]
                else None
            )
            students.append(
                StudentResult(
                    participation_id=row["participation_id"],
                    roster_entry_id=row["roster_entry_id"],
                    student_id=row["student_id"],
                    student_name=row["student_name"],
                    student_code=row["student_code"],
                    exam_number=row["exam_number"],
                    class_id=row["class_id"],
                    class_name=row["class_name"],
                    session_paper_id=row["session_paper_id"],
                    paper_version_id=row["paper_version_id"],
                    variant=paper["variant_code"] if paper else None,
                    script_id=script["answer_script_id"] if script else None,
                    script_version_id=row["script_version_id"],
                    attendance_status=row["attendance"],
                    status="complete" if row["attendance"] == "present" else "absent",
                    reviewed_count=len(question_rows),
                    required_count=len(question_rows) if row["attendance"] == "present" else None,
                    subtotal=str(row["total"]) if row["total"] is not None else None,
                    total=str(row["total"]) if row["total"] is not None else None,
                    max_points=str(row["max_points"]) if row["max_points"] is not None else None,
                    questions=[
                        QuestionResult(
                            mapping_id=q["mapping_id"],
                            question_id=q["legacy_question_id"],
                            number=q["question_number"],
                            max_points=str(q["max_points"]),
                            status="reviewed",
                            points=str(q["points"]),
                            review_id=q["review_id"],
                            omr_decision_id=q["omr_decision_id"],
                            evidence_source="omr" if q["omr_decision_id"] else "teacher_review",
                        )
                        for q in question_rows
                    ],
                )
            )
        result = FormalPreview(
            session_id=sid,
            event_id=event["id"],
            event_title=event["title"],
            session_title=session["title"],
            roster_revision_id=version["roster_revision_id"],
            generated_at=datetime.now(UTC),
            published=version["status"] == "published",
            snapshot_sha256=version["snapshot_sha256"],
            classes=[dict(class_id=key, class_name=value) for key, value in names.items()],
            students=students,
            offset=0,
            limit=LIMIT,
            total_students=len(students),
            has_more=False,
        )
        return result, dict(
            id=str(version["id"]),
            version_no=version["version_no"],
            status=version["status"],
            published_at=version["published_at"].isoformat() if version["published_at"] else None,
            supersedes_id=str(version["supersedes_id"]) if version["supersedes_id"] else None,
        )


def statistics(value):
    """Use one published snapshot; class attendance and each paper are separate."""
    classes, papers = {}, {}
    for student in value.students:
        cid = str(student.class_id)
        cls = classes.setdefault(
            cid,
            dict(
                class_id=cid,
                class_name=student.class_name,
                total=0,
                present=0,
                absent=0,
                missing_reviews=0,
            ),
        )
        cls["total"] += 1
        if student.attendance_status == "absent":
            cls["absent"] += 1
            continue
        cls["present"] += 1
        key = (cid, str(student.paper_version_id), student.variant, student.max_points)
        group = papers.setdefault(
            key,
            dict(
                class_id=cid,
                class_name=student.class_name,
                paper_version_id=str(student.paper_version_id),
                variant=student.variant,
                max_points=student.max_points,
                scores=[],
                questions={},
            ),
        )
        group["scores"].append(Decimal(student.total))
        for question in student.questions:
            bucket = group["questions"].setdefault(
                str(question.mapping_id),
                dict(
                    mapping_id=str(question.mapping_id),
                    number=question.number,
                    max_points=question.max_points,
                    points=[],
                ),
            )
            bucket["points"].append(Decimal(question.points))
    for cls in classes.values():
        cls["completion_rate"] = str(
            (Decimal(cls["present"]) / cls["total"] * 100).quantize(Decimal("0.01"))
        )
    output = []
    for group in papers.values():
        scores = group.pop("scores")
        total = len(scores)
        counts = {"0-59%": 0, "60-69%": 0, "70-79%": 0, "80-89%": 0, "90-100%": 0}
        for score in scores:
            ratio = score / Decimal(group["max_points"]) * 100
            label = (
                "0-59%"
                if ratio < 60
                else "60-69%"
                if ratio < 70
                else "70-79%"
                if ratio < 80
                else "80-89%"
                if ratio < 90
                else "90-100%"
            )
            counts[label] += 1
        questions = []
        for item in group.pop("questions").values():
            points = item.pop("points")
            item["score_rate"] = str(
                (sum(points) / len(points) / Decimal(item["max_points"]) * 100).quantize(
                    Decimal("0.01")
                )
            )
            questions.append(item)
        group.update(
            count=total,
            average=str((sum(scores) / total).quantize(Decimal("0.01"))),
            highest=str(max(scores)),
            lowest=str(min(scores)),
            distribution=counts,
            questions=questions,
        )
        output.append(group)
    return {
        "classes": list(classes.values()),
        "papers": output,
        "completion_definition": "到考且已完成全部复核人数 / 名单人数；缺考不计零分",
    }


async def enable_operator(db, actor_id, target_id, request_id, approval_ref):
    """Controlled deployment only; never exposed through teacher HTTP."""
    from huipi_cloud.modules.resource_security import service as security
    from huipi_cloud.modules.resource_security.models import SecurityGrant

    from .permissions import RESULT_DEPLOYER_ROLE

    if not approval_ref.strip() or len(approval_ref) > 120:
        raise AccessError("final_approval_required", 422)
    principal = await g.parsing.actor(db, actor_id)
    await db.rollback()
    async with security.operation(
        db,
        principal,
        request_id,
        "bootstrap",
        dict(target=str(target_id), approval_ref=approval_ref, role="exam_result_deployer"),
    ) as op:
        scope = await security.installation_scope(db)
        await security.require_permission(db, principal, "security.grants.manage", scope.id)
        if op.replay is not None:
            return op.replay
        await g.parsing.actor(db, target_id)
        gid = uuid4()
        await db.execute(
            insert(SecurityGrant).values(
                id=gid, user_id=target_id, role_version_id=RESULT_DEPLOYER_ROLE, scope_id=scope.id
            )
        )
        op.result = {"grant_id": str(gid)}
        return op.result
