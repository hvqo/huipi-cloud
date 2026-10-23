"""Manual decisions with bounded source verification outside short DB transactions."""

import asyncio
import hashlib
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete, func, insert, or_, select, update

from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.exam_rosters.models import RosterEntrySnapshot, RosterRevision
from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.scans import service as scans
from huipi_cloud.modules.scans.router import verified_download

from .models import (
    AnswerScript,
    AnswerScriptOperation,
    AnswerScriptPage,
    AnswerScriptVersion,
    CurrentPageAllocation,
)
from .schemas import ScriptReceipt

A, V, L, C, OPS = (
    m.__table__
    for m in (
        AnswerScript,
        AnswerScriptVersion,
        AnswerScriptPage,
        CurrentPageAllocation,
        AnswerScriptOperation,
    )
)
R, E = RosterRevision.__table__, RosterEntrySnapshot.__table__
S, EV, SP, PV = exams.S, exams.E, exams.B, exams.P
P, F, B = scans.P, scans.F, scans.B
DENIED = "resource_not_found_or_forbidden"
MAX_FILES = 8
MAX_SOURCE_BYTES = 256 * 1024 * 1024


async def authorize(db, principal, sid, *actions):
    await exams.authorize(db, principal, "exam_session", sid, actions)


async def http_authorize(db, principal, params, action):
    sid = params.get("exam_session_id")
    if sid:
        sid = UUID(str(sid))
    else:
        sid = await db.scalar(
            select(A.c.exam_session_id).where(A.c.id == UUID(str(params["script_id"])))
        )
    if sid is None:
        raise AccessError(DENIED, 404)
    await authorize(db, principal, sid, action)
    return sid


async def identity_access(db, principal, sid):
    try:
        await authorize(db, principal, sid, "exam.roster.read")
        return True
    except AccessError as error:
        if error.status_code != 404:
            raise
        return False


async def source_rows(db, sid, ids, *, bounded=True):
    rows = (
        (
            await db.execute(
                select(
                    P,
                    F.c.batch_id,
                    F.c.original_filename,
                    F.c.sha256.label("file_sha256"),
                    F.c.size_bytes.label("file_size"),
                    F.c.bucket,
                    F.c.object_key.label("file_key"),
                )
                .join(F, F.c.id == P.c.file_id)
                .join(B, B.c.id == F.c.batch_id)
                .where(P.c.id.in_(ids), B.c.exam_session_id == sid, F.c.status == "stored")
                .order_by(P.c.id)
            )
        )
        .mappings()
        .all()
    )
    by_id = {r["id"]: dict(r) for r in rows}
    if len(by_id) != len(ids):
        raise AccessError(DENIED, 404)
    rows = [by_id[i] for i in ids]
    files = {r["file_id"]: r["file_size"] for r in rows}
    if bounded and (
        len(files) > MAX_FILES
        or sum(files.values()) + sum(r["size_bytes"] for r in rows) > MAX_SOURCE_BYTES
    ):
        raise AccessError("script_source_limit", 413)
    return rows


def public_page(row):
    return dict(
        scan_page_id=row["id"],
        file_id=row["file_id"],
        original_filename=row.get("original_filename", ""),
        batch_id=row["batch_id"],
        source_page_index=row["page_index"],
        page_sha256=row["sha256"],
        file_sha256=row["file_sha256"],
        allocated_script_id=row.get("allocated_script_id"),
    )


def manifest_digest(rows):
    # Display labels and transient ownership are not source identity. Array order is identity.
    return exams.digest(
        [
            {
                key: value
                for key, value in public_page(row).items()
                if key not in {"original_filename", "allocated_script_id"}
            }
            for row in rows
        ]
    )


async def verify_sources(storage, rows):
    """Read-only bounded streaming. No object delete/upload or DB transaction during I/O."""
    try:
        async with asyncio.timeout(60):
            with tempfile.TemporaryDirectory(prefix="huipi-script-verify-") as directory:
                root = Path(directory)
                done = set()
                for row in rows:
                    if row["file_id"] not in done:
                        await verified_download(
                            storage,
                            dict(
                                bucket=row["bucket"],
                                object_key=row["file_key"],
                                size_bytes=row["file_size"],
                                sha256=row["file_sha256"],
                            ),
                            32 * 1024 * 1024,
                            root,
                        )
                        done.add(row["file_id"])
                    await verified_download(storage, row, 2 * 1024 * 1024, root)
    except (StorageUnavailableError, OSError, TimeoutError):
        raise AccessError("script_source_unavailable", 503) from None


async def identity_check(db, sid, request):
    # Parent-filter every client selector before reading it: no cross-session existence oracle.
    rr = (
        (
            await db.execute(
                select(R)
                .where(R.c.id == request.roster_revision_id, R.c.exam_session_id == sid)
                .with_for_update(read=True)
            )
        )
        .mappings()
        .first()
    )
    if rr is None:
        raise AccessError(DENIED, 404)
    if not rr["is_current"] or rr["status"] != "frozen":
        raise AccessError("script_roster_changed", 409)
    entry = (
        (
            await db.execute(
                select(E).where(
                    E.c.id == request.roster_entry_id,
                    E.c.exam_session_id == sid,
                    E.c.roster_revision_id == rr["id"],
                )
            )
        )
        .mappings()
        .first()
    )
    paper = (
        (
            await db.execute(
                select(SP)
                .where(SP.c.id == request.session_paper_id, SP.c.exam_session_id == sid)
                .with_for_update(read=True)
            )
        )
        .mappings()
        .first()
    )
    if entry is None or paper is None:
        raise AccessError(DENIED, 404)
    if paper["status"] != "active" or (
        entry["session_paper_id"] is not None and entry["session_paper_id"] != paper["id"]
    ):
        raise AccessError("script_variant_mismatch", 409)
    version = await exams.row(db, PV, paper["paper_version_id"], share=True)
    if version["status"] != "locked":
        raise AccessError("script_paper_unavailable", 409)
    return entry, paper


async def receipt(db, principal, rid, digest):
    old = (
        (
            await db.execute(
                select(OPS).where(OPS.c.actor_user_id == principal.user_id, OPS.c.request_id == rid)
            )
        )
        .mappings()
        .first()
    )
    if old:
        if old["payload_sha256"] != digest:
            raise AccessError("script_request_conflict", 409)
        return ScriptReceipt.model_validate(old["result"])
    return None


async def write(db, principal, storage, sid, request, script_id=None):
    action = request.action if script_id else "create"
    digest = exams.digest(
        dict(
            session=str(sid),
            script=str(script_id),
            action=action,
            body=request.model_dump(mode="json"),
        )
    )
    # Preflight: no long transaction across storage. Replay still requires CURRENT authority.
    async with scans.transaction(db):
        await check_write_authority(db, principal, sid, action, script_id)
        old = await receipt(db, principal, request.request_id, digest)
        if old:
            return old
        await scans.writable_session(db, sid)
        if script_id:
            root = await exams.row(db, A, script_id)
            if root["exam_session_id"] != sid:
                raise AccessError(DENIED, 404)
            if root["revision"] != request.expected_revision:
                raise AccessError("script_revision_conflict", 409)
        rows = await source_rows(db, sid, request.page_ids)
        if action == "confirm":
            await identity_check(db, sid, request)
    if action == "confirm":
        await verify_sources(storage, rows)
    async with scans.transaction(db):
        await check_write_authority(db, principal, sid, action, script_id)
        key = int.from_bytes(
            hashlib.sha256(f"script:{principal.user_id}:{request.request_id}".encode()).digest()[
                :8
            ],
            signed=True,
        )
        await db.execute(select(func.pg_advisory_xact_lock(key)))
        old = await receipt(db, principal, request.request_id, digest)
        if old:
            return old
        await scans.writable_session(db, sid)
        root = await exams.row(db, A, script_id, lock=True) if script_id else None
        if root and (
            root["exam_session_id"] != sid or root["revision"] != request.expected_revision
        ):
            raise AccessError("script_revision_conflict", 409)
        if root and root["revision"] >= 100:
            raise AccessError("script_version_limit", 409)
        latest = await source_rows(db, sid, request.page_ids)
        if manifest_digest(rows) != manifest_digest(latest):
            raise AccessError("script_source_changed", 409)
        # Session serializes writers including roster freeze. Lock pages in stable ID order.
        await db.execute(
            select(P.c.id)
            .where(P.c.id.in_(request.page_ids))
            .order_by(P.c.id)
            .with_for_update(read=True)
        )
        owners = (
            (await db.execute(select(C).where(C.c.scan_page_id.in_(request.page_ids))))
            .mappings()
            .all()
        )
        if any(r["answer_script_id"] != script_id for r in owners):
            raise AccessError("script_page_already_allocated", 409)
        identity = dict(
            roster_revision_id=None,
            roster_entry_id=None,
            participation_id=None,
            session_paper_id=None,
        )
        if action == "confirm":
            entry, _ = await identity_check(db, sid, request)
            identity = dict(
                roster_revision_id=request.roster_revision_id,
                roster_entry_id=entry["id"],
                participation_id=entry["participation_id"],
                session_paper_id=request.session_paper_id,
            )
        elif action == "revoke":
            previous_pages = list(
                (
                    await db.scalars(
                        select(L.c.scan_page_id)
                        .where(L.c.version_id == root["current_version_id"])
                        .order_by(L.c.ordinal)
                    )
                ).all()
            )
            if previous_pages != request.page_ids:
                raise AccessError("script_revoke_must_preserve_pages", 409)
        aid, vid = script_id or uuid4(), uuid4()
        number = root["revision"] + 1 if root else 1
        state = "confirmed" if action == "confirm" else "revoked" if action == "revoke" else "draft"
        if root:
            await db.execute(delete(C).where(C.c.answer_script_id == aid))
            await db.execute(
                update(A)
                .where(A.c.id == aid)
                .values(
                    current_version_id=vid,
                    revision=number,
                    current_participation_id=identity["participation_id"],
                )
            )
        else:
            count = await db.scalar(
                select(func.count()).select_from(A).where(A.c.exam_session_id == sid)
            )
            if count >= 4000:
                raise AccessError("script_session_limit", 409)
            await db.execute(
                insert(A).values(
                    id=aid,
                    exam_session_id=sid,
                    current_version_id=vid,
                    revision=1,
                    created_by_user_id=principal.user_id,
                )
            )
        await db.execute(
            insert(V).values(
                id=vid,
                answer_script_id=aid,
                exam_session_id=sid,
                version_no=number,
                previous_version_id=root["current_version_id"] if root else None,
                status=state,
                completeness=request.completeness,
                reason=request.reason,
                manifest_sha256=manifest_digest(rows),
                actor_user_id=principal.user_id,
                **identity,
            )
        )
        await db.execute(
            insert(L),
            [
                dict(
                    version_id=vid,
                    ordinal=i,
                    scan_page_id=r["id"],
                    file_id=r["file_id"],
                    source_page_index=r["page_index"],
                    page_sha256=r["sha256"],
                    file_sha256=r["file_sha256"],
                )
                for i, r in enumerate(rows)
            ],
        )
        if state != "revoked":
            await db.execute(
                insert(C),
                [
                    dict(
                        scan_page_id=r["id"],
                        answer_script_id=aid,
                        version_id=vid,
                        exam_session_id=sid,
                    )
                    for r in rows
                ],
            )
        result = ScriptReceipt(script_id=aid, version_id=vid, revision=number, status=state)
        await db.execute(
            insert(OPS).values(
                id=uuid4(),
                actor_user_id=principal.user_id,
                request_id=request.request_id,
                action=action,
                payload_sha256=digest,
                result=result.model_dump(mode="json"),
            )
        )
        return result


async def check_write_authority(db, principal, sid, action, script_id):
    actions = {"exam.script.edit"}
    if action == "confirm":
        actions |= {"exam.script.confirm", "exam.identity.verify"}
        await authorize(db, principal, sid, "exam.roster.read")
    elif script_id:
        root = await exams.row(db, A, script_id)
        if root["exam_session_id"] != sid:
            raise AccessError(DENIED, 404)
        current = await exams.row(db, V, root["current_version_id"])
        if current["status"] == "confirmed":
            actions.add("exam.script.confirm")
    await authorize(db, principal, sid, *actions)


async def pages(db, principal, sid, offset, limit, batch_id=None, unclaimed=False):
    async with scans.transaction(db):
        await authorize(db, principal, sid, "exam.script.read", "scan.file.read")
        query = (
            select(P.c.id)
            .join(F, F.c.id == P.c.file_id)
            .join(B, B.c.id == F.c.batch_id)
            .outerjoin(C, C.c.scan_page_id == P.c.id)
            .where(B.c.exam_session_id == sid, F.c.status == "stored")
        )
        if batch_id:
            query = query.where(B.c.id == batch_id)
        if unclaimed:
            query = query.where(C.c.scan_page_id.is_(None))
        total = await db.scalar(select(func.count()).select_from(query.subquery()))
        ids = list(
            (
                await db.scalars(
                    query.order_by(B.c.created_at, F.c.created_at, P.c.page_index, P.c.id)
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        )
        rows = await source_rows(db, sid, ids, bounded=False) if ids else []
        owners = (
            dict(
                (
                    await db.execute(
                        select(C.c.scan_page_id, C.c.answer_script_id).where(
                            C.c.scan_page_id.in_(ids)
                        )
                    )
                ).all()
            )
            if ids
            else {}
        )
        return dict(
            items=[public_page(r) | {"allocated_script_id": owners.get(r["id"])} for r in rows],
            total=total,
            offset=offset,
            limit=limit,
        )


async def candidates(db, principal, sid, query, offset, limit):
    async with scans.transaction(db):
        await authorize(db, principal, sid, "exam.script.read")
        await authorize(db, principal, sid, "exam.roster.read")
        roster = (
            (
                await db.execute(
                    select(R).where(
                        R.c.exam_session_id == sid, R.c.is_current, R.c.status == "frozen"
                    )
                )
            )
            .mappings()
            .first()
        )
        items, total = [], 0
        if roster:
            stmt = select(E).where(E.c.roster_revision_id == roster["id"])
            # Literal containment, not user-supplied LIKE wildcards or SQL.
            if query:
                stmt = stmt.where(
                    or_(
                        *(
                            col.contains(query, autoescape=True)
                            for col in (
                                E.c.student_name,
                                E.c.student_code,
                                E.c.exam_number,
                                E.c.class_name,
                            )
                        )
                    )
                )
            total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
            items = [
                candidate(r)
                for r in (
                    await db.execute(
                        stmt.order_by(E.c.class_code, E.c.student_code, E.c.id)
                        .offset(offset)
                        .limit(limit)
                    )
                ).mappings()
            ]
        papers = (
            (
                await db.execute(
                    select(
                        SP.c.id.label("session_paper_id"), SP.c.paper_version_id, SP.c.variant_code
                    )
                    .join(PV, PV.c.id == SP.c.paper_version_id)
                    .where(
                        SP.c.exam_session_id == sid,
                        SP.c.status == "active",
                        PV.c.status == "locked",
                    )
                    .order_by(SP.c.variant_code)
                )
            )
            .mappings()
            .all()
        )
        return dict(
            exam_session_id=sid,
            roster_revision_id=roster["id"] if roster else None,
            roster_version_no=roster["version_no"] if roster else None,
            items=items,
            papers=papers,
            total=total,
            offset=offset,
            limit=limit,
        )


def candidate(row):
    return dict(
        roster_entry_id=row["id"],
        **{
            k: row[k]
            for k in (
                "participation_id",
                "student_name",
                "student_code",
                "class_name",
                "exam_number",
                "session_paper_id",
            )
        },
    )


async def version_read(db, version, visible):
    rows = (
        (
            await db.execute(
                select(L, F.c.original_filename, B.c.id.label("batch_id"))
                .join(F, F.c.id == L.c.file_id)
                .join(B, B.c.id == F.c.batch_id)
                .where(L.c.version_id == version["id"])
                .order_by(L.c.ordinal)
            )
        )
        .mappings()
        .all()
    )
    result = {
        k: version[k]
        for k in (
            "id",
            "version_no",
            "previous_version_id",
            "status",
            "completeness",
            "reason",
            "manifest_sha256",
            "created_at",
        )
    }
    if not visible:
        result["reason"] = "操作说明仅向具有名单权限的人员展示"
    # A scan-only user cannot enumerate roster or participation identifiers through script history.
    result.update(
        {
            k: version[k] if visible else None
            for k in (
                "roster_revision_id",
                "roster_entry_id",
                "participation_id",
                "session_paper_id",
            )
        }
    )
    result["identity"] = (
        candidate(await exams.row(db, E, version["roster_entry_id"]))
        if visible and version["roster_entry_id"]
        else None
    )
    result["pages"] = [
        dict(
            scan_page_id=r["scan_page_id"],
            file_id=r["file_id"],
            original_filename=r["original_filename"],
            batch_id=r["batch_id"],
            source_page_index=r["source_page_index"],
            page_sha256=r["page_sha256"],
            file_sha256=r["file_sha256"],
            allocated_script_id=None,
        )
        for r in rows
    ]
    return result


async def readiness(db, root, version):
    if version["status"] != "confirmed":
        return "unclaimed" if version["status"] == "draft" else "revoked"
    rr = await exams.row(db, R, version["roster_revision_id"])
    sp = await exams.row(db, SP, version["session_paper_id"])
    pv = await exams.row(db, PV, sp["paper_version_id"])
    session = await exams.row(db, S, root["exam_session_id"])
    event = await exams.row(db, EV, session["exam_event_id"])
    if not rr["is_current"] or rr["status"] != "frozen":
        return "roster_changed"
    if sp["status"] != "active" or pv["status"] != "locked":
        return "paper_unavailable"
    if session["lifecycle"] != "ready" or event["lifecycle"] not in {"draft", "active"}:
        return "session_unavailable"
    if version["completeness"] != "complete":
        return "incomplete"
    return "identity_confirmed"  # Does not mean parsed, graded, or a durable bridge permit.


async def inspect_value(db, principal, root):
    version = await exams.row(db, V, root["current_version_id"])
    visible = await identity_access(db, principal, root["exam_session_id"])
    return dict(
        script_id=root["id"],
        exam_session_id=root["exam_session_id"],
        revision=root["revision"],
        current=await version_read(db, version, visible),
        readiness=await readiness(db, root, version),
        identity_visible=visible,
    )


async def inspect(db, principal, aid):
    async with scans.transaction(db):
        root = await exams.row(db, A, aid)
        await authorize(db, principal, root["exam_session_id"], "exam.script.read")
        return await inspect_value(db, principal, root)


async def listing(db, principal, sid, offset, limit):
    async with scans.transaction(db):
        await authorize(db, principal, sid, "exam.script.read")
        total = await db.scalar(
            select(func.count()).select_from(A).where(A.c.exam_session_id == sid)
        )
        roots = (
            (
                await db.execute(
                    select(A)
                    .where(A.c.exam_session_id == sid)
                    .order_by(A.c.created_at, A.c.id)
                    .offset(offset)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        return dict(
            items=[await inspect_value(db, principal, r) for r in roots],
            total=total,
            offset=offset,
            limit=limit,
        )


async def history(db, principal, aid, offset, limit):
    async with scans.transaction(db):
        root = await exams.row(db, A, aid)
        await authorize(db, principal, root["exam_session_id"], "exam.script.read")
        visible = await identity_access(db, principal, root["exam_session_id"])
        versions = (
            (
                await db.execute(
                    select(V)
                    .where(V.c.answer_script_id == aid)
                    .order_by(V.c.version_no.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            .mappings()
            .all()
        )
        return dict(
            items=[await version_read(db, v, visible) for v in versions],
            total=root["revision"],
            offset=offset,
            limit=limit,
        )


async def manifest(db, principal, storage, aid):
    async with scans.transaction(db):
        root = await exams.row(db, A, aid)
        await authorize(
            db, principal, root["exam_session_id"], "exam.script.read", "exam.identity.verify"
        )
        await authorize(db, principal, root["exam_session_id"], "exam.roster.read")
        version = await exams.row(db, V, root["current_version_id"])
        if await readiness(db, root, version) != "identity_confirmed":
            raise AccessError("script_not_ready", 409)
        ids = list(
            (
                await db.scalars(
                    select(L.c.scan_page_id)
                    .where(L.c.version_id == version["id"])
                    .order_by(L.c.ordinal)
                )
            ).all()
        )
        rows = await source_rows(db, root["exam_session_id"], ids)
    await verify_sources(storage, rows)
    async with scans.transaction(db):
        # Reauthorize and establish a fresh epoch after storage I/O.
        await authorize(
            db, principal, root["exam_session_id"], "exam.script.read", "exam.identity.verify"
        )
        await authorize(db, principal, root["exam_session_id"], "exam.roster.read")
        await scans.writable_session(db, root["exam_session_id"])
        latest = await exams.row(db, A, aid, share=True)
        if (
            latest["current_version_id"] != version["id"]
            or await readiness(db, latest, version) != "identity_confirmed"
        ):
            raise AccessError("script_source_changed", 409)
        sp = await exams.row(db, SP, version["session_paper_id"], share=True)
        pv = await exams.row(db, PV, sp["paper_version_id"], share=True)
        if pv["status"] != "locked":
            raise AccessError("script_paper_unavailable", 409)
        return dict(
            script_id=aid,
            answer_script_version_id=version["id"],
            exam_session_id=root["exam_session_id"],
            roster_revision_id=version["roster_revision_id"],
            roster_entry_id=version["roster_entry_id"],
            participation_id=version["participation_id"],
            session_paper_id=sp["id"],
            paper_version_id=pv["id"],
            manifest_sha256=version["manifest_sha256"],
            pages=[public_page(r) for r in rows],
            validated_at=datetime.now(UTC),
        )


async def enable_operator(db, actor_id, target_id, request_id, approval_ref):
    """Explicit deployment-only bootstrap, never an HTTP or Agent tool."""
    from datetime import UTC, datetime, timedelta

    from huipi_cloud.modules.identity.models import User
    from huipi_cloud.modules.identity.protocol import Principal
    from huipi_cloud.modules.resource_security import service as security
    from huipi_cloud.modules.resource_security.models import SecurityGrant

    from .permissions import SCRIPT_DEPLOYER_ROLE

    if not approval_ref.strip() or len(approval_ref) > 120:
        raise AccessError("script_approval_required", 422)
    actor = await db.get(User, actor_id)
    if actor is None:
        raise AccessError(DENIED, 404)
    principal = Principal(
        user_id=actor.id,
        issuer=actor.issuer,
        subject=actor.subject,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    await db.rollback()
    digest = exams.digest(dict(target=str(target_id), approval_ref=approval_ref))
    async with scans.transaction(db):
        scope = await security.installation_scope(db)
        await security.require_permission(db, principal, "security.grants.manage", scope.id)
        await db.execute(
            select(
                func.pg_advisory_xact_lock(
                    int.from_bytes(
                        hashlib.sha256(f"script:{actor_id}:{request_id}".encode()).digest()[:8],
                        signed=True,
                    )
                )
            )
        )
        old = (
            (
                await db.execute(
                    select(OPS).where(
                        OPS.c.actor_user_id == actor_id,
                        OPS.c.request_id == request_id,
                        OPS.c.action == "enable_operator",
                    )
                )
            )
            .mappings()
            .first()
        )
        if old:
            if old["payload_sha256"] != digest:
                raise AccessError("script_request_conflict", 409)
            return old["result"]
        target = await db.scalar(
            select(User).where(User.id == target_id, User.is_active).with_for_update(read=True)
        )
        if target is None:
            raise AccessError(DENIED, 404)
        identifier = uuid4()
        await db.execute(
            insert(SecurityGrant).values(
                id=identifier,
                user_id=target_id,
                role_version_id=SCRIPT_DEPLOYER_ROLE,
                scope_id=scope.id,
            )
        )
        result = dict(grant_id=str(identifier))
        await db.execute(
            insert(OPS).values(
                id=uuid4(),
                actor_user_id=actor_id,
                request_id=request_id,
                action="enable_operator",
                payload_sha256=digest,
                result=result,
            )
        )
        return result
