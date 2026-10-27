"""Fixed SQL projections only: no exception tickets, raw student text, or hidden grants."""

from datetime import UTC, datetime

from sqlalchemy import String, Uuid, and_, case, cast, exists, func, literal, or_, select, union_all

from huipi_cloud.modules.exam_configuration import service as config
from huipi_cloud.modules.exam_grading import service as grading
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.marking_tasks import service as tasks
from huipi_cloud.modules.scans import service as scans

from .schemas import RecoveryItem, RecoveryView

scripts, parsing = grading.parsing.scripts, grading.parsing
S, EV, A, V, R, SP, PV = (getattr(scripts, k) for k in ("S", "EV", "A", "V", "R", "SP", "PV"))
B, F, P, C, J, W, SC, AR, K, GP, T = (
    scans.B,
    scans.F,
    scans.P,
    scripts.C,
    parsing.J,
    grading.W,
    grading.S,
    grading.AR,
    parsing.K,
    grading.GP,
    tasks.T,
)
GROUPS = {
    "files": {"scan.batch.read"},
    "pages": {"scan.batch.read", "scan.file.read"},
    "scripts": {"exam.script.read"},
    "parsing": {"exam.parse.read", "exam.script.read", "exam.identity.verify"},
    "grading": {"exam.mark.read", "exam.script.read", "exam.identity.verify"},
    "manage_tasks": {"exam.task.manage"},
    "own_tasks": {"exam.task.work"},
}
DESCRIPTIONS = {
    "upload_failed": ("文件接收失败", "原件或页面处理未完成；原上传人可选择同一PDF重试。"),
    "upload_interrupted": ("文件接收中断", "接收租约已过期，尚未安全完成；请核对后重传。"),
    "unclaimed_page": ("未认领扫描页", "页面尚未组成当前答卷，请人工分卷并确认身份。"),
    "duplicate_page_check": (
        "疑似重复页待核验",
        "同场次存在相同预览字节的页面；可能为重复扫描或正常空白页，不能自动删除。",
    ),
    "identity_pending": ("答卷身份待确认", "尚未建立可信学生与实际卷别关联，不代表缺考。"),
    "incomplete_script": (
        "答卷不完整，需核对缺页及页序",
        "操作者标记了不完整。补页、调序后须重新确认；系统不自动推断缺少哪页。",
    ),
    "source_changed": (
        "答卷来源已变化",
        "名单、场次或卷版状态已变化，请核对当前名单与用卷后重新确认。",
    ),
    "parsing_failed": (
        "解析失败",
        "请核对原页。临时资源故障排除后可重新确认版本并发起；损坏来源应先补扫。",
    ),
    "parsing_invalidated": ("解析来源已失效", "旧任务不能作为当前解析结果；请重新核对身份及来源。"),
    "parsing_not_started": ("已确认答卷待解析", "答卷完整且身份已确认，尚未发起当前版本解析。"),
    "answer_unconfirmed": (
        "作答区域待人工核对",
        "实际作答尚未确认或存在疑问；不能自动评分或补零。",
    ),
    "answer_absent": (
        "本题已核对未观察到作答",
        "教师已确认未观察到作答，须保留人工判断；不是整卷缺考，系统不自动补零。",
    ),
    "scoring_failed": (
        "AI评分失败",
        "候选未通过或调用失败；请先核对作答，再明确重新确认并发起。不会自动重试。",
    ),
    "scoring_abstained": ("AI评分弃权", "模型未给出可用候选，须人工核对证据；弃权不是零分。"),
    "scoring_invalidated": ("评分来源已失效", "旧候选不能恢复为当前分数；请从当前答卷重新核对。"),
    "task_invalidated": (
        "阅卷任务来源或授权失效",
        "旧任务不能计入成绩；未完成任务可撤回，已完成任务保留历史，请在有效新答卷重新派单并人工跟踪。",
    ),
}


def allowed(grants, actions, row):
    return config.allowed(grants, actions, "exam_session", row["id"], row["exam_event_id"])


async def scope(db, p, sid):
    grants = await config.actor_grants(db, p)
    row = (await db.execute(select(S).where(S.c.id == sid))).mappings().first()
    if row is None:
        raise AccessError("resource_not_found_or_forbidden", 404)
    caps = {k: allowed(grants, codes, row) for k, codes in GROUPS.items()}
    roster = allowed(grants, {"exam.roster.read"}, row)
    caps["parsing"] &= roster
    caps["grading"] &= roster
    own = caps["own_tasks"] and bool(
        await db.scalar(
            select(T.c.id)
            .where(
                T.c.exam_session_id == sid,
                T.c.assignee_user_id == p.user_id,
                T.c.is_current,
                T.c.status.not_in(["cancelled", "superseded"]),
            )
            .limit(1)
        )
    )
    caps["own_tasks"] = own
    if not any(caps.values()):
        raise AccessError("resource_not_found_or_forbidden", 404)
    caps["upload"] = allowed(grants, {"scan.file.upload"}, row)
    return row, caps


async def sessions(db, p, offset=0):
    async with scans.transaction(db):
        grants = await config.actor_grants(db, p)
        filters = []
        for key, codes in GROUPS.items():
            complete = [(s, c) for s, c in grants if codes <= c]
            criterion = config.scope_filter(complete, sorted(codes)[0], "exam_session")
            if key == "own_tasks":
                criterion = and_(
                    criterion,
                    exists(
                        select(T.c.id).where(
                            T.c.exam_session_id == S.c.id,
                            T.c.assignee_user_id == p.user_id,
                            T.c.is_current,
                            T.c.status.not_in(["cancelled", "superseded"]),
                        )
                    ),
                )
            filters.append(criterion)
        rows = (
            (
                await db.execute(
                    select(S.c.id, S.c.title, EV.c.title.label("event_title"))
                    .join(EV, EV.c.id == S.c.exam_event_id)
                    .where(or_(*filters))
                    .order_by(S.c.created_at, S.c.id)
                    .offset(offset)
                    .limit(21)
                )
            )
            .mappings()
            .all()
        )
        return dict(items=[dict(r) for r in rows[:20]], has_next=len(rows) > 20)


def projection(kind, identifier, title, when, **fields):
    columns = dict(kind=kind, resource_id=identifier, title=title, updated_at=when)
    for k in (
        "batch_id",
        "file_id",
        "page_id",
        "script_id",
        "workspace_id",
        "task_id",
        "retry_request_id",
        "original_filename",
    ):
        columns[k] = fields.get(
            k, cast(literal(None), String if k == "original_filename" else Uuid)
        )
    columns["failure_code"] = fields.get("failure_code", cast(literal(None), String))
    return select(*[v.label(k) for k, v in columns.items()])


def has_grant(user_id, codes):
    """The whole action set must belong to one sealed, current grant in this real scope."""
    from huipi_cloud.modules.identity.models import User
    from huipi_cloud.modules.resource_security.models import (
        SecurityGrant,
        SecurityRolePermission,
        SecurityRoleVersion,
        SecurityScope,
    )

    gr, rp, rv, ss, u = (
        m.__table__.alias()
        for m in (SecurityGrant, SecurityRolePermission, SecurityRoleVersion, SecurityScope, User)
    )
    return exists(
        select(gr.c.id)
        .select_from(gr)
        .join(rp, rp.c.role_version_id == gr.c.role_version_id)
        .join(rv, rv.c.id == gr.c.role_version_id)
        .join(ss, ss.c.id == gr.c.scope_id)
        .join(u, u.c.id == gr.c.user_id)
        .where(
            gr.c.user_id == user_id,
            gr.c.revoked_at.is_(None),
            rv.c.sealed_at.is_not(None),
            u.c.is_active,
            rp.c.permission_code.in_(codes),
            or_(
                ss.c.kind == "installation",
                and_(ss.c.kind == "exam_session", ss.c.exam_session_id == S.c.id),
                and_(ss.c.kind == "exam_event", ss.c.exam_event_id == S.c.exam_event_id),
            ),
        )
        .group_by(gr.c.id)
        .having(func.count() == len(codes))
        .correlate(S, J, T, SC)
    )


def current_source():
    return and_(
        A.c.current_version_id == J.c.answer_script_version_id,
        R.c.is_current,
        R.c.status == "frozen",
        SP.c.status == "active",
        PV.c.status == "locked",
        S.c.lifecycle == "ready",
        EV.c.lifecycle.in_(["draft", "active"]),
        has_grant(
            J.c.requested_by_user_id,
            {"exam.parse.start", "exam.script.read", "exam.identity.verify"},
        ),
        has_grant(J.c.requested_by_user_id, {"exam.roster.read"}),
    )


def script_join(query):
    return (
        query.select_from(A)
        .join(V, V.c.id == A.c.current_version_id)
        .join(S, S.c.id == A.c.exam_session_id)
        .join(EV, EV.c.id == S.c.exam_event_id)
        .outerjoin(R, R.c.id == V.c.roster_revision_id)
        .outerjoin(SP, SP.c.id == V.c.session_paper_id)
        .outerjoin(PV, PV.c.id == SP.c.paper_version_id)
    )


def workspace_join(query):
    return (
        query.select_from(W)
        .join(J, J.c.id == W.c.parsing_job_id)
        .join(A, A.c.id == J.c.answer_script_id)
        .join(V, V.c.id == J.c.answer_script_version_id)
        .join(R, R.c.id == V.c.roster_revision_id)
        .join(SP, SP.c.id == V.c.session_paper_id)
        .join(PV, PV.c.id == SP.c.paper_version_id)
        .join(S, S.c.id == A.c.exam_session_id)
        .join(EV, EV.c.id == S.c.exam_event_id)
    )


def queries(p, sid, caps):
    values = []
    if caps["files"]:
        kind = case((F.c.status == "failed", "upload_failed"), else_="upload_interrupted")
        retry_id = case(
            (and_(F.c.uploaded_by_user_id == p.user_id, literal(caps["upload"])), F.c.request_id),
            else_=None,
        )
        values.append(
            projection(
                kind,
                F.c.id,
                F.c.original_filename,
                F.c.updated_at,
                batch_id=F.c.batch_id,
                file_id=F.c.id,
                retry_request_id=retry_id,
                original_filename=F.c.original_filename,
                failure_code=F.c.failure_code,
            )
            .select_from(F)
            .join(B, B.c.id == F.c.batch_id)
            .where(
                B.c.exam_session_id == sid,
                or_(
                    F.c.status == "failed",
                    and_(F.c.status == "receiving", F.c.lease_expires_at <= func.clock_timestamp()),
                ),
            )
        )
    if caps["pages"]:
        p2, f2, b2 = P.alias("other_page"), F.alias("other_file"), B.alias("other_batch")
        duplicate = exists(
            select(p2.c.id)
            .select_from(p2)
            .join(f2, f2.c.id == p2.c.file_id)
            .join(b2, b2.c.id == f2.c.batch_id)
            .where(
                b2.c.exam_session_id == sid,
                f2.c.status == "stored",
                p2.c.sha256 == P.c.sha256,
                p2.c.id != P.c.id,
            )
        )
        kind = case((duplicate, "duplicate_page_check"), else_="unclaimed_page")
        values.append(
            projection(
                kind,
                P.c.id,
                F.c.original_filename
                + literal(" · 第")
                + (P.c.page_index + 1).cast(String)
                + literal("页"),
                P.c.created_at,
                batch_id=F.c.batch_id,
                file_id=F.c.id,
                page_id=P.c.id,
            )
            .select_from(P)
            .join(F, F.c.id == P.c.file_id)
            .join(B, B.c.id == F.c.batch_id)
            .outerjoin(C, C.c.scan_page_id == P.c.id)
            .where(B.c.exam_session_id == sid, F.c.status == "stored", C.c.scan_page_id.is_(None))
        )
    if caps["scripts"]:
        stale = or_(
            ~R.c.is_current,
            R.c.status != "frozen",
            SP.c.status != "active",
            PV.c.status != "locked",
            S.c.lifecycle != "ready",
            EV.c.lifecycle.not_in(["draft", "active"]),
        )
        kind = case(
            (V.c.status == "draft", "identity_pending"),
            (stale, "source_changed"),
            (V.c.completeness == "incomplete", "incomplete_script"),
            else_=None,
        )
        q = projection(
            kind,
            A.c.id,
            literal("答卷 · 第") + V.c.version_no.cast(String) + literal("版"),
            V.c.created_at,
            script_id=A.c.id,
        )
        values.append(
            script_join(q).where(
                A.c.exam_session_id == sid, V.c.status != "revoked", kind.is_not(None)
            )
        )
    if caps["parsing"]:
        latest = (
            select(J.c.id)
            .where(J.c.answer_script_id == A.c.id)
            .order_by(J.c.created_at.desc(), J.c.id.desc())
            .limit(1)
            .correlate(A)
            .scalar_subquery()
        )
        kind = case(
            (or_(J.c.id.is_(None), J.c.answer_script_version_id != V.c.id), "parsing_not_started"),
            (J.c.status == "failed", "parsing_failed"),
            (J.c.status == "invalidated", "parsing_invalidated"),
            else_=None,
        )
        q = projection(
            kind,
            A.c.id,
            literal("答卷解析"),
            V.c.created_at,
            script_id=A.c.id,
            failure_code=J.c.failure_code,
        )
        q = script_join(q).outerjoin(J, J.c.id == latest)
        values.append(
            q.where(
                A.c.exam_session_id == sid,
                V.c.status == "confirmed",
                V.c.completeness == "complete",
                R.c.is_current,
                SP.c.status == "active",
                PV.c.status == "locked",
                S.c.lifecycle == "ready",
                kind.is_not(None),
            )
        )
    if caps["grading"] or caps["own_tasks"]:
        score_id = (
            select(SC.c.id)
            .where(SC.c.workspace_id == W.c.id)
            .order_by(SC.c.created_at.desc(), SC.c.id.desc())
            .limit(1)
            .correlate(W)
            .scalar_subquery()
        )
        review_id = (
            select(AR.c.id)
            .where(AR.c.submission_id == K.c.submission_id, AR.c.question_id == W.c.question_id)
            .order_by(AR.c.revision.desc())
            .limit(1)
            .correlate(W, K)
            .scalar_subquery()
        )
        kind = case(
            (~current_source(), "scoring_invalidated"),
            (or_(AR.c.id.is_(None), AR.c.decision == "uncertain"), "answer_unconfirmed"),
            (AR.c.decision == "response_absent", "answer_absent"),
            (
                and_(
                    SC.c.id.is_not(None),
                    or_(
                        SC.c.answer_review_id != AR.c.id,
                        ~or_(
                            and_(
                                has_grant(
                                    SC.c.requested_by_user_id,
                                    {
                                        "exam.mark.read",
                                        "exam.score.request",
                                        "exam.script.read",
                                        "exam.identity.verify",
                                    },
                                ),
                                has_grant(SC.c.requested_by_user_id, {"exam.roster.read"}),
                            ),
                            and_(
                                T.c.id.is_not(None),
                                T.c.assignee_user_id == SC.c.requested_by_user_id,
                                T.c.status.in_(["in_progress", "completed"]),
                                has_grant(SC.c.requested_by_user_id, {"exam.task.work"}),
                            ),
                        ),
                    ),
                ),
                "scoring_invalidated",
            ),
            (SC.c.status == "failed", "scoring_failed"),
            (SC.c.status == "invalidated", "scoring_invalidated"),
            (GP.c.grading_status == "grading_abstained", "scoring_abstained"),
            else_=None,
        )
        q = workspace_join(
            projection(
                kind,
                W.c.id,
                literal("第") + grading.Q.c.question_number.cast(String) + literal("题批改"),
                W.c.created_at,
                script_id=A.c.id,
                workspace_id=W.c.id,
                task_id=T.c.id,
                failure_code=SC.c.failure_code,
            )
        )
        q = (
            q.join(grading.Q, grading.Q.c.id == W.c.question_id)
            .join(K, K.c.job_id == J.c.id)
            .outerjoin(AR, AR.c.id == review_id)
            .outerjoin(SC, SC.c.id == score_id)
            .outerjoin(GP, GP.c.id == SC.c.proposal_id)
            .outerjoin(T, and_(T.c.workspace_id == W.c.id, T.c.is_current))
        )
        if not caps["grading"]:
            q = q.where(
                T.c.assignee_user_id == p.user_id, T.c.status.not_in(["cancelled", "superseded"])
            )
        values.append(
            q.where(A.c.exam_session_id == sid, A.c.current_version_id == V.c.id, kind.is_not(None))
        )
    if caps["manage_tasks"] or caps["own_tasks"]:
        q = workspace_join(
            projection(
                literal("task_invalidated"),
                T.c.id,
                literal("阅卷任务"),
                T.c.assigned_at,
                script_id=A.c.id,
                task_id=T.c.id,
                workspace_id=W.c.id,
            )
        )
        q = q.join(T, T.c.workspace_id == W.c.id).where(
            T.c.is_current,
            T.c.status.not_in(["cancelled", "superseded"]),
            T.c.exam_session_id == sid,
            or_(~current_source(), ~has_grant(T.c.assignee_user_id, {"exam.task.work"})),
        )
        if not caps["manage_tasks"]:
            q = q.where(T.c.assignee_user_id == p.user_id)
        values.append(q)
    return values


async def inspect(db, p, sid, offset=0, kind=None, *, limit=25):
    async with scans.transaction(db):
        row, caps = await scope(db, p, sid)
        event = await scripts.exams.row(db, EV, row["exam_event_id"])
        qs = queries(p, sid, caps)
        items, counts, total = [], {}, 0
        if qs:
            all_rows = union_all(*qs).subquery()
            counts = dict(
                (
                    await db.execute(
                        select(all_rows.c.kind, func.count()).group_by(all_rows.c.kind)
                    )
                ).all()
            )
            query = select(all_rows)
            if kind:
                query = query.where(all_rows.c.kind == kind)
            total = sum(counts.values()) if kind is None else counts.get(kind, 0)
            rows = (
                (
                    await db.execute(
                        query.order_by(
                            all_rows.c.updated_at.desc(), all_rows.c.kind, all_rows.c.resource_id
                        )
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            for r in rows:
                title, reason = DESCRIPTIONS[r["kind"]]
                value = dict(r)
                failure = value.pop("failure_code", None)
                failures = {
                    "scan_storage_unavailable": "文件存储暂不可用。",
                    "scan_upload_interrupted": "文件接收未完成。",
                    "parser_unavailable": "本地解析资源暂不可用。",
                    "execution_timeout": "解析超过单次时限。",
                    "input_materialization_failed": "答卷来源暂时无法读取或组装。",
                    "attempts_exhausted": "已达到本轮解析尝试上限。",
                    "corrupt_input": "文件损坏或无法识别，请补扫。",
                    "invalid_input": "解析输入不符合要求，请核对原件。",
                    "invalid_result": "解析结果未通过完整性校验。",
                    "model_total_mismatch": "模型总分与分项不一致，候选已拒绝。",
                    "evidence_quote_mismatch": "模型引用未通过原文校验，候选已拒绝。",
                    "execution_outcome_unknown": "执行结果未知，须先核对状态。",
                }
                if failure:
                    reason = failures.get(failure, "本次处理未完成安全校验。") + reason
                value["title"] = title + " · " + r["title"]
                items.append(
                    RecoveryItem(
                        **value,
                        reason=reason,
                        state="manual_review"
                        if r["kind"]
                        in {
                            "duplicate_page_check",
                            "source_changed",
                            "parsing_invalidated",
                            "task_invalidated",
                        }
                        else "pending",
                    )
                )
        results = {}
        if caps["files"]:
            results["stored_files"] = await db.scalar(
                select(func.count())
                .select_from(F)
                .join(B, B.c.id == F.c.batch_id)
                .where(B.c.exam_session_id == sid, F.c.status == "stored")
            )
        if caps["scripts"]:
            results["confirmed_complete_scripts"] = await db.scalar(
                script_join(select(func.count())).where(
                    A.c.exam_session_id == sid,
                    V.c.status == "confirmed",
                    V.c.completeness == "complete",
                    R.c.is_current,
                    R.c.status == "frozen",
                    SP.c.status == "active",
                    PV.c.status == "locked",
                    S.c.lifecycle == "ready",
                    EV.c.lifecycle.in_(["draft", "active"]),
                )
            )
        if caps["parsing"]:
            results["parsed_scripts"] = await db.scalar(
                script_join(select(func.count()))
                .join(J, J.c.answer_script_version_id == V.c.id)
                .where(A.c.exam_session_id == sid, J.c.status == "succeeded", current_source())
            )
        return RecoveryView(
            session_id=sid,
            session_title=row["title"],
            event_title=event["title"],
            items=items,
            counts=counts,
            total=total,
            offset=offset,
            limit=limit,
            has_next=limit > 0 and offset + len(items) < total,
            capabilities=caps,
            results=results,
            generated_at=datetime.now(UTC),
        )
