"""Bounded single-marker allocation, fresh task scope and existing GradeReview completion."""

import hashlib
from contextlib import asynccontextmanager
from uuid import UUID, uuid4, uuid5

from sqlalchemy import case, func, insert, select, update

from huipi_cloud.modules.exam_configuration import service as configuration
from huipi_cloud.modules.exam_grading import service as g
from huipi_cloud.modules.exam_grading.schemas import Intent
from huipi_cloud.modules.identity.models import User
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.resource_security import service as security
from huipi_cloud.modules.scans import service as scans

from . import access
from .models import MarkingOperation

T, OPS = access.T, MarkingOperation.__table__
DENIED = "resource_not_found_or_forbidden"


async def manager(db, p, sid):
    await access.authorize(db, p, sid, "exam.task.manage")


async def task(db, tid):
    return await g.row(db, T, tid)


@asynccontextmanager
async def operation(db, p, sid, request, action, *, resource_id=None):
    digest = g.json_sha256(
        dict(
            action=action,
            session=str(sid),
            resource=str(resource_id) if resource_id else None,
            body=request.model_dump(mode="json"),
        )
    )
    key = int.from_bytes(
        hashlib.sha256(f"marking:{p.user_id}:{request.request_id}".encode()).digest()[:8],
        signed=True,
    )
    await db.execute(select(func.pg_advisory_xact_lock(key)))
    old = (
        (
            await db.execute(
                select(OPS).where(
                    OPS.c.actor_user_id == p.user_id, OPS.c.request_id == request.request_id
                )
            )
        )
        .mappings()
        .first()
    )
    if old and old["payload_sha256"] != digest:
        raise AccessError("marking_request_conflict", 409)
    box = {"replay": dict(old["result"]) if old else None}
    yield box
    if old is None:
        await db.execute(
            insert(OPS).values(
                id=uuid4(),
                actor_user_id=p.user_id,
                exam_session_id=sid,
                request_id=request.request_id,
                action=action,
                payload_sha256=digest,
                result=box["result"],
            )
        )


async def sessions(db, p, offset=0):
    async with scans.transaction(db):
        grants = await configuration.actor_grants(db, p)
        rows = (
            (
                await db.execute(
                    select(
                        g.parsing.scripts.S.c.id,
                        g.parsing.scripts.S.c.title,
                        g.parsing.scripts.EV.c.title.label("event_title"),
                    )
                    .join(
                        g.parsing.scripts.EV,
                        g.parsing.scripts.EV.c.id == g.parsing.scripts.S.c.exam_event_id,
                    )
                    .where(configuration.scope_filter(grants, "exam.task.manage", "exam_session"))
                    .order_by(g.parsing.scripts.S.c.id)
                    .offset(offset)
                    .limit(21)
                )
            )
            .mappings()
            .all()
        )
        return {"items": [dict(r) for r in rows[:20]], "has_next": len(rows) > 20}


async def teachers(db, p, sid):
    async with scans.transaction(db):
        await manager(db, p, sid)
        # Server evaluates real current grants, never JWT roles or client IDs as authority.
        from huipi_cloud.modules.resource_security.models import (
            SecurityGrant,
            SecurityRolePermission,
        )

        ids = (
            await db.scalars(
                select(User.id)
                .join(SecurityGrant, SecurityGrant.user_id == User.id)
                .join(
                    SecurityRolePermission,
                    SecurityRolePermission.role_version_id == SecurityGrant.role_version_id,
                )
                .where(
                    User.is_active,
                    SecurityGrant.revoked_at.is_(None),
                    SecurityRolePermission.permission_code == "exam.task.work",
                )
                .distinct()
                .order_by(User.id)
                .limit(101)
            )
        ).all()
        if len(ids) > 100:
            raise AccessError("marking_teacher_limit", 413)
        values = []
        for uid in ids:
            try:
                await access.authorize(db, await g.parsing.actor(db, uid), sid, "exam.task.work")
            except AccessError as e:
                if e.status_code != 404:
                    raise
                continue
            values.append({"user_id": uid, "label": "教师账号 " + uid.hex[:8]})
        return {"items": values}


async def pool(db, p, sid, offset=0):
    async with scans.transaction(db):
        await manager(db, p, sid)
        await scans.writable_session(db, sid)
        jobs = (
            (
                await db.execute(
                    select(g.parsing.J.c.id)
                    .join(
                        g.parsing.scripts.A,
                        g.parsing.scripts.A.c.id == g.parsing.J.c.answer_script_id,
                    )
                    .where(
                        g.parsing.J.c.exam_session_id == sid,
                        g.parsing.J.c.status == "succeeded",
                        g.parsing.scripts.A.c.current_version_id
                        == g.parsing.J.c.answer_script_version_id,
                    )
                    .order_by(g.parsing.J.c.created_at, g.parsing.J.c.id)
                    .offset(offset)
                    .limit(6)
                )
            )
            .scalars()
            .all()
        )
        result = []
        for jid in jobs[:5]:
            try:
                c = await g.context(db, p, jid)
            except AccessError as e:
                if e.status_code in {413, 503}:
                    raise
                result.append({"job_id": jid, "status": "source_stale", "questions": []})
                continue
            entry = await g.row(db, g.parsing.scripts.E, c.source[1]["roster_entry_id"])
            workspaces = (
                (await db.execute(select(g.W).where(g.W.c.parsing_job_id == jid))).mappings().all()
            )
            byq = {w["question_id"]: w for w in workspaces}
            questions = []
            for q in c.questions:
                w = byq.get(q.id)
                t = await access.current(db, w["id"]) if w else None
                questions.append(
                    dict(
                        question_id=q.id,
                        number=q.question_number,
                        workspace_id=w["id"] if w else None,
                        task=g.parsing.scripts.exams.public(t) if t else None,
                    )
                )
            result.append(
                dict(
                    job_id=jid,
                    status="ready",
                    student_name=entry["student_name"],
                    student_code=entry["student_code"],
                    class_name=entry["class_name"],
                    variant=c.source[3]["variant_code"],
                    questions=questions,
                )
            )
        return dict(items=result, has_next=len(jobs) > 5)


async def prepare(db, p, storage, sid, jid, request):
    async with scans.transaction(db):
        await manager(db, p, sid)
        j = await g.row(db, g.parsing.J, jid)
        if j["exam_session_id"] != sid:
            raise AccessError(DENIED, 404)
    return await g.initialize(
        db, p, storage, jid, Intent(request_id=uuid5(request.request_id, str(jid)))
    )


async def assign(db, p, sid, request):
    if len({v.workspace_id for v in request.targets}) != len(request.targets):
        raise AccessError("marking_duplicate_workspace", 422)
    async with scans.transaction(db):
        await manager(db, p, sid)
        await access.authorize(
            db, await g.parsing.actor(db, request.assignee_user_id), sid, "exam.task.work"
        )
        await scans.writable_session(db, sid)
        async with operation(db, p, sid, request, "assign") as op:
            if op["replay"]:
                for tid in op["replay"]["task_ids"]:
                    if not (await task(db, UUID(tid)))["is_current"]:
                        raise AccessError("marking_assignment_changed", 409)
                return op["replay"]
            ids = []
            for target in sorted(request.targets, key=lambda t: t.workspace_id):
                c = await g.workspace_context(db, p, target.workspace_id)
                if c.job["exam_session_id"] != sid:
                    raise AccessError(DENIED, 404)
                previous = await access.current(db, target.workspace_id, lock=True)
                if (
                    previous
                    and (
                        previous["id"] != target.expected_task_id
                        or previous["revision"] != target.expected_revision
                    )
                ) or (
                    not previous
                    and (target.expected_task_id is not None or target.expected_revision != 0)
                ):
                    raise AccessError("marking_revision_conflict", 409)
                if (
                    previous
                    and previous["status"] in {"in_progress", "completed"}
                    and (not request.force or len(request.reason.strip()) < 5)
                ):
                    raise AccessError("marking_force_reason_required", 409)
                if previous:
                    await db.execute(
                        update(T)
                        .where(T.c.id == previous["id"])
                        .values(
                            is_current=False, status="superseded", revision=previous["revision"] + 1
                        )
                    )
                tid = uuid4()
                await db.execute(
                    insert(T).values(
                        id=tid,
                        exam_session_id=sid,
                        workspace_id=target.workspace_id,
                        generation=previous["generation"] + 1 if previous else 1,
                        supersedes_id=previous["id"] if previous else None,
                        assignee_user_id=request.assignee_user_id,
                        assigned_by_user_id=p.user_id,
                        reason=request.reason,
                    )
                )
                ids.append(str(tid))
            op["result"] = {"task_ids": ids}
            return op["result"]


async def own(db, p, tid, *, lock=False):
    t = await task(db, tid)
    await access.authorize(db, p, t["exam_session_id"], "exam.task.work")
    await scans.writable_session(db, t["exam_session_id"])
    t = (
        (await db.execute(select(T).where(T.c.id == tid).with_for_update())).mappings().one()
        if lock
        else t
    )
    if (
        t["assignee_user_id"] != p.user_id
        or not t["is_current"]
        or t["status"] in {"cancelled", "superseded"}
    ):
        raise AccessError(DENIED, 404)
    return t


async def claim(db, p, tid, request):
    async with scans.transaction(db):
        t = await own(db, p, tid, lock=True)
        async with operation(db, p, t["exam_session_id"], request, "claim", resource_id=tid) as op:
            if op["replay"]:
                return op["replay"]
            if t["revision"] != request.expected_revision or t["status"] != "assigned":
                raise AccessError("marking_already_claimed_or_changed", 409)
            await db.execute(
                update(T)
                .where(T.c.id == tid)
                .values(
                    status="in_progress",
                    started_at=func.clock_timestamp(),
                    revision=t["revision"] + 1,
                )
            )
            await g.workspace_context(db, p, t["workspace_id"])
            op["result"] = {"task_id": str(tid), "revision": t["revision"] + 1}
            return op["result"]


async def complete(db, p, storage, tid, request):
    async with scans.transaction(db):
        t = await own(db, p, tid)
    d = await g.details(db, p, storage, t["workspace_id"])
    async with scans.transaction(db):
        t = await own(db, p, tid, lock=True)
        await g.workspace_context(db, p, t["workspace_id"])
        async with operation(
            db, p, t["exam_session_id"], request, "complete", resource_id=tid
        ) as op:
            if op["replay"]:
                return op["replay"]
            if t["revision"] != request.expected_revision or t["status"] != "in_progress":
                raise AccessError("marking_revision_conflict", 409)
            proposal = d["visual_proposal"] or d["proposal"]
            history = d["visual_history"] or d["history"]
            if d["scoring_status"] != "succeeded" or not proposal or not history:
                raise AccessError("marking_review_required", 409)
            job = await g.row(db, g.S, UUID(d["scoring_job_id"]))
            await g.scoring_context(db, p, job)
            await g.scoring_context(
                db,
                await g.parsing.actor(db, job["requested_by_user_id"]),
                job,
                "exam.score.request",
            )
            review = (
                (
                    await db.execute(
                        select(g.GR)
                        .where(g.GR.c.proposal_id == job["proposal_id"])
                        .order_by(g.GR.c.review_revision.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            if (
                not review
                or str(review["review_id"]) != history[-1]["review_id"]
                or review["reviewer_user_id"] != p.user_id
                or review["created_at"] < t["assigned_at"]
                or review["review_status"] not in {"accepted", "revised"}
            ):
                raise AccessError("marking_fresh_teacher_review_required", 409)
            await db.execute(
                update(T)
                .where(T.c.id == tid)
                .values(
                    status="completed",
                    completed_at=func.clock_timestamp(),
                    review_id=review["review_id"],
                    revision=t["revision"] + 1,
                )
            )
            op["result"] = {"task_id": str(tid), "revision": t["revision"] + 1}
            return op["result"]


async def cancel(db, p, tid, request):
    async with scans.transaction(db):
        t = await task(db, tid)
        await manager(db, p, t["exam_session_id"])
        await scans.writable_session(db, t["exam_session_id"])
        t = await access.current(db, t["workspace_id"], lock=True)
        if t["id"] != tid:
            raise AccessError("marking_assignment_changed", 409)
        async with operation(db, p, t["exam_session_id"], request, "cancel", resource_id=tid) as op:
            if op["replay"]:
                return op["replay"]
            if t["revision"] != request.expected_revision:
                raise AccessError("marking_revision_conflict", 409)
            # Completed facts are retained by superseding, not overwritten or cancelled.
            if t["status"] == "completed":
                raise AccessError("marking_completed_requires_reassignment", 409)
            await db.execute(
                update(T)
                .where(T.c.id == tid)
                .values(status="cancelled", revision=t["revision"] + 1)
            )
            op["result"] = {"task_id": str(tid), "revision": t["revision"] + 1}
            return op["result"]


async def item(db, p, t, *, manager_view=False):
    j = await g.row(db, g.parsing.J, (await g.row(db, g.W, t["workspace_id"]))["parsing_job_id"])
    q = await g.row(db, g.Q, (await g.row(db, g.W, t["workspace_id"]))["question_id"])
    root = await g.row(db, g.parsing.scripts.A, j["answer_script_id"])
    version = await g.row(db, g.parsing.scripts.V, j["answer_script_version_id"])
    entry = await g.row(db, g.parsing.scripts.E, version["roster_entry_id"])
    status = t["status"]
    try:
        # Assigned work is not yet readable through the grading API, but its current
        # source is checked by the existing authenticated parsing intent.
        await g.parsing.context(
            db,
            await g.parsing.actor(db, j["requested_by_user_id"]),
            root["id"],
            version_id=version["id"],
        )
        await access.authorize(
            db,
            await g.parsing.actor(db, t["assignee_user_id"]),
            t["exam_session_id"],
            "exam.task.work",
        )
    except AccessError as e:
        if e.status_code in {413, 503}:
            raise
        status = "invalidated"
    return dict(
        task_id=t["id"],
        workspace_id=t["workspace_id"],
        job_id=j["id"],
        question_number=q["question_number"],
        student_name=entry["student_name"],
        student_code=entry["student_code"],
        class_name=entry["class_name"],
        assignee_user_id=t["assignee_user_id"],
        status=status,
        stored_status=t["status"],
        revision=t["revision"],
        generation=t["generation"],
        review_id=t["review_id"],
    )


def progress_query():
    """One SQL source-freshness projection; score evidence is still checked by results."""
    scripts = g.parsing.scripts
    fresh = (
        (scripts.A.c.current_version_id == g.parsing.J.c.answer_script_version_id)
        & scripts.R.c.is_current
        & (scripts.R.c.status == "frozen")
        & (scripts.SP.c.status == "active")
        & (scripts.PV.c.status == "locked")
        & (scripts.S.c.lifecycle == "ready")
        & User.is_active
    )
    state = case(
        (T.c.status == "cancelled", "cancelled"), (~fresh, "invalidated"), else_=T.c.status
    )
    query = (
        select(T.c.assignee_user_id, state.label("status"), func.count().label("count"))
        .select_from(T)
        .join(g.W, g.W.c.id == T.c.workspace_id)
        .join(g.parsing.J, g.parsing.J.c.id == g.W.c.parsing_job_id)
        .join(scripts.A, scripts.A.c.id == g.parsing.J.c.answer_script_id)
        .join(scripts.V, scripts.V.c.id == g.parsing.J.c.answer_script_version_id)
        .join(scripts.R, scripts.R.c.id == scripts.V.c.roster_revision_id)
        .join(scripts.SP, scripts.SP.c.id == scripts.V.c.session_paper_id)
        .join(scripts.PV, scripts.PV.c.id == scripts.SP.c.paper_version_id)
        .join(scripts.S, scripts.S.c.id == T.c.exam_session_id)
        .join(User, User.id == T.c.assignee_user_id)
        .where(T.c.is_current)
        .group_by(T.c.assignee_user_id, state)
        .order_by(T.c.assignee_user_id)
    )
    return query


async def my_tasks(db, p, offset=0):
    async with scans.transaction(db):
        grants = await configuration.actor_grants(db, p)
        scope = configuration.scope_filter(grants, "exam.task.work", "exam_session")
        rows = (
            (
                await db.execute(
                    select(T)
                    .join(g.parsing.scripts.S, g.parsing.scripts.S.c.id == T.c.exam_session_id)
                    .where(T.c.assignee_user_id == p.user_id, T.c.is_current, scope)
                    .order_by(T.c.assigned_at, T.c.id)
                    .offset(offset)
                    .limit(26)
                )
            )
            .mappings()
            .all()
        )
        values = [await item(db, p, t) for t in rows[:25]]
        counts = (
            (await db.execute(progress_query().where(T.c.assignee_user_id == p.user_id, scope)))
            .mappings()
            .all()
        )
        return dict(items=values, has_next=len(rows) > 25, counts=[dict(c) for c in counts])


async def inspect(db, p, tid):
    async with scans.transaction(db):
        t = await own(db, p, tid)
        return await item(db, p, t)


async def progress(db, p, sid, offset=0):
    async with scans.transaction(db):
        await manager(db, p, sid)
        rows = (
            (
                await db.execute(
                    select(T)
                    .where(T.c.exam_session_id == sid, T.c.is_current)
                    .order_by(T.c.assignee_user_id, T.c.id)
                    .offset(offset)
                    .limit(26)
                )
            )
            .mappings()
            .all()
        )
        values = [await item(db, p, t, manager_view=True) for t in rows[:25]]
        counts = (
            (await db.execute(progress_query().where(T.c.exam_session_id == sid))).mappings().all()
        )
        adjusted = []
        for counter in counts:
            counter = dict(counter)
            try:
                await access.authorize(
                    db,
                    await g.parsing.actor(db, counter["assignee_user_id"]),
                    sid,
                    "exam.task.work",
                )
            except AccessError as error:
                if error.status_code != 404:
                    raise
                counter["status"] = "invalidated"
            adjusted.append(counter)
        return dict(
            items=values,
            has_next=len(rows) > 25,
            counts=adjusted,
            counts_note="任务提交记录；成绩预览另行核验评分证据，不代表发布成绩",
        )


async def enable_operator(db, actor_id, target_id, request_id, approval_ref):
    """Deployment approval only. Does not expose a public security-grant endpoint."""
    from huipi_cloud.modules.resource_security.models import SecurityGrant

    from .permissions import DEPLOYER_ROLE

    if not approval_ref.strip() or len(approval_ref) > 120:
        raise AccessError("marking_approval_required", 422)
    p = await g.parsing.actor(db, actor_id)
    await db.rollback()
    async with security.operation(
        db,
        p,
        request_id,
        "bootstrap",
        dict(target=str(target_id), approval_ref=approval_ref, role="marking_deployer"),
    ) as op:
        scope = await security.installation_scope(db)
        await security.require_permission(db, p, "security.grants.manage", scope.id)
        if op.replay is not None:
            return op.replay
        await g.parsing.actor(db, target_id)
        gid = uuid4()
        await db.execute(
            insert(SecurityGrant).values(
                id=gid, user_id=target_id, role_version_id=DEPLOYER_ROLE, scope_id=scope.id
            )
        )
        op.result = {"grant_id": str(gid)}
        return op.result


async def preview_metadata(db, p, tid, pid):
    async with scans.transaction(db):
        t = await own(db, p, tid)
        c = await g.workspace_context(db, p, t["workspace_id"])
        page = next((r for r in c.source[2] if r["id"] == pid), None)
        if page is None:
            raise AccessError(DENIED, 404)
        return dict(page)
