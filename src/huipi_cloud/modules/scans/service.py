"""Protected intake transactions; private immutable objects and fenced retry attempts."""

import asyncio
import hashlib
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from fastapi import UploadFile
from sqlalchemy import and_, exists, func, insert, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.exam_rosters.models import RosterRevision
from huipi_cloud.modules.exams import service as exams
from huipi_cloud.modules.exams.models import ExamEvent, ExamSession
from huipi_cloud.modules.identity import service as identity
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.organization.models import Subject
from huipi_cloud.modules.resource_security.models import (
    PermissionDefinition,
    SecurityGrant,
    SecurityRolePermission,
    SecurityRoleVersion,
    SecurityScope,
)
from huipi_cloud.modules.submissions.uploads import stage_upload

from .models import ScanBatch, ScanFile, ScanOperation, ScanPage
from .pdf import previews
from .schemas import BatchRead, FileRead, PageRead

B, F, P, OPS = (m.__table__ for m in (ScanBatch, ScanFile, ScanPage, ScanOperation))
S, EV, R = (m.__table__ for m in (ExamSession, ExamEvent, RosterRevision))
log = logging.getLogger(__name__)
DENIED = "resource_not_found_or_forbidden"


async def authorize(db, principal, session_id, action):
    await exams.authorize(db, principal, "exam_session", session_id, {action})


async def http_authorize(db, principal, params, action):
    if "exam_session_id" in params:
        sid = UUID(str(params["exam_session_id"]))
    else:
        query = select(B.c.exam_session_id)
        if "batch_id" in params:
            query = query.where(B.c.id == UUID(str(params["batch_id"])))
        elif "file_id" in params:
            query = query.join(F, F.c.batch_id == B.c.id).where(
                F.c.id == UUID(str(params["file_id"]))
            )
        else:
            query = (
                query.join(F, F.c.batch_id == B.c.id)
                .join(P, P.c.file_id == F.c.id)
                .where(P.c.id == UUID(str(params["page_id"])))
            )
        sid = await db.scalar(query)
        if sid is None:
            raise AccessError(DENIED, 404)
    await authorize(db, principal, sid, action)
    return sid


@asynccontextmanager
async def transaction(db):
    committing = False
    try:
        await db.begin()
        await db.execute(
            select(
                func.set_config("statement_timeout", "8000", True),
                func.set_config("lock_timeout", "5000", True),
            )
        )
        yield
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
                raise AccessError("scan_write_conflict", 409) from None
            raise AccessError(
                "scan_commit_outcome_unknown" if committing else "scan_unavailable", 503
            ) from None
        raise


async def audit(db, actor, request_id, action, digest, result, attempt=0):
    await db.execute(
        insert(OPS).values(
            id=uuid4(),
            actor_user_id=actor,
            request_id=request_id,
            action=action,
            attempt_number=attempt,
            payload_sha256=digest,
            result=result,
        )
    )


async def writable_session(db, sid):
    value = await exams.row(db, S, sid)
    event = await exams.row(db, EV, value["exam_event_id"], share=True)
    value = await exams.row(db, S, sid, lock=True)
    if value["lifecycle"] != "ready" or event["lifecycle"] not in {"draft", "active"}:
        raise AccessError("scan_session_not_ready", 409)
    return value


async def create_batch(db, principal, sid, request):
    digest = exams.digest(dict(session=str(sid), title=request.title))
    async with transaction(db):
        await authorize(db, principal, sid, "scan.batch.create")
        await db.execute(
            select(
                func.pg_advisory_xact_lock(
                    int.from_bytes(
                        hashlib.sha256(
                            f"scan:{principal.user_id}:{request.request_id}".encode()
                        ).digest()[:8],
                        signed=True,
                    )
                )
            )
        )
        old = (
            (
                await db.execute(
                    select(OPS).where(
                        OPS.c.actor_user_id == principal.user_id,
                        OPS.c.request_id == request.request_id,
                        OPS.c.action == "batch_create",
                    )
                )
            )
            .mappings()
            .first()
        )
        if old:
            if old["payload_sha256"] != digest:
                raise AccessError("scan_request_conflict", 409)
            return BatchRead.model_validate(await exams.row(db, B, UUID(old["result"]["batch_id"])))
        await writable_session(db, sid)
        if (
            await db.scalar(select(func.count()).select_from(B).where(B.c.exam_session_id == sid))
            >= 100
        ):
            raise AccessError("scan_batch_limit", 409)
        roster_id = await db.scalar(
            select(R.c.id).where(R.c.exam_session_id == sid, R.c.is_current, R.c.status == "frozen")
        )
        row = (
            (
                await db.execute(
                    insert(B)
                    .values(
                        id=uuid4(),
                        exam_session_id=sid,
                        roster_revision_id=roster_id,
                        title=request.title,
                        created_by_user_id=principal.user_id,
                    )
                    .returning(B)
                )
            )
            .mappings()
            .one()
        )
        await audit(
            db,
            principal.user_id,
            request.request_id,
            "batch_create",
            digest,
            dict(batch_id=str(row["id"])),
        )
        return BatchRead.model_validate(row)


async def sessions(db, principal, offset, limit, *, session_id=None):
    async with transaction(db):
        await identity.refresh_principal(db, principal)
        G, SC, RP, RV, PD = (
            m.__table__
            for m in (
                SecurityGrant,
                SecurityScope,
                SecurityRolePermission,
                SecurityRoleVersion,
                PermissionDefinition,
            )
        )
        allowed = exists(
            select(G.c.id)
            .join(SC, SC.c.id == G.c.scope_id)
            .join(RV, RV.c.id == G.c.role_version_id)
            .join(RP, RP.c.role_version_id == RV.c.id)
            .join(PD, PD.c.code == RP.c.permission_code)
            .where(
                G.c.user_id == principal.user_id,
                G.c.revoked_at.is_(None),
                RV.c.sealed_at.is_not(None),
                PD.c.enabled,
                PD.c.code == "scan.batch.read",
                or_(
                    and_(SC.c.kind == "installation", SC.c.installation_id == 1),
                    and_(SC.c.kind == "exam_event", SC.c.exam_event_id == S.c.exam_event_id),
                    and_(SC.c.kind == "exam_session", SC.c.exam_session_id == S.c.id),
                ),
            )
        )
        roster = exists(select(R.c.id).where(R.c.exam_session_id == S.c.id, R.c.is_current))
        rows = (
            (
                await db.execute(
                    select(
                        S.c.id,
                        EV.c.title.label("event_title"),
                        S.c.title,
                        Subject.display_name.label("subject"),
                        S.c.starts_at,
                        S.c.lifecycle,
                        roster.label("roster_frozen"),
                    )
                    .join(EV, EV.c.id == S.c.exam_event_id)
                    .join(Subject, Subject.id == S.c.subject_id)
                    .where(allowed, S.c.id == session_id if session_id else True)
                    .order_by(S.c.starts_at.desc(), S.c.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        return dict(
            items=[dict(r) for r in rows[:limit]],
            offset=offset,
            limit=limit,
            truncated=len(rows) > limit,
            limits=dict(
                max_file_bytes=settings.scan_max_file_bytes, max_pages=settings.scan_max_pages
            ),
        )


async def batches(db, principal, sid, offset, limit):
    async with transaction(db):
        await authorize(db, principal, sid, "scan.batch.read")
        rows = (
            (
                await db.execute(
                    select(B)
                    .where(B.c.exam_session_id == sid)
                    .order_by(B.c.created_at.desc(), B.c.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        return dict(
            items=[BatchRead.model_validate(r) for r in rows[:limit]], truncated=len(rows) > limit
        )


async def inspect_batch(db, principal, bid):
    async with transaction(db):
        await http_authorize(db, principal, {"batch_id": bid}, "scan.batch.read")
        return BatchRead.model_validate(await exams.row(db, B, bid))


async def files(db, principal, bid, offset, limit):
    async with transaction(db):
        await http_authorize(db, principal, {"batch_id": bid}, "scan.batch.read")
        rows = (
            (
                await db.execute(
                    select(F)
                    .where(F.c.batch_id == bid)
                    .order_by(F.c.created_at.desc(), F.c.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        return dict(
            items=[FileRead.model_validate(r) for r in rows[:limit]], truncated=len(rows) > limit
        )


async def file_info(db, principal, fid):
    async with transaction(db):
        await http_authorize(db, principal, {"file_id": fid}, "scan.batch.read")
        return FileRead.model_validate(await exams.row(db, F, fid))


async def page_list(db, principal, fid, offset, limit):
    async with transaction(db):
        await http_authorize(db, principal, {"file_id": fid}, "scan.file.read")
        row = await exams.row(db, F, fid)
        if row["status"] != "stored":
            raise AccessError("scan_file_not_ready", 409)
        rows = (
            (
                await db.execute(
                    select(P)
                    .where(P.c.file_id == fid)
                    .order_by(P.c.page_index)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        return dict(
            items=[PageRead.model_validate(r) for r in rows[:limit]], truncated=len(rows) > limit
        )


async def reserve(db, principal, storage, bid, rid, staged):
    digest = exams.digest(
        dict(
            batch=str(bid),
            sha256=staged.sha256,
            size=staged.size_bytes,
            filename=staged.filename,
            source="manual_pdf",
        )
    )
    async with transaction(db):
        await http_authorize(db, principal, {"batch_id": bid}, "scan.file.upload")
        batch = await exams.row(db, B, bid)
        # Actor/request advisory also serializes accidental reuse across two batches.
        await db.execute(
            select(
                func.pg_advisory_xact_lock(
                    int.from_bytes(
                        hashlib.sha256(f"scan:{principal.user_id}:{rid}".encode()).digest()[:8],
                        signed=True,
                    )
                )
            )
        )
        await writable_session(db, batch["exam_session_id"])
        await exams.row(db, B, bid, lock=True)
        old = (
            (
                await db.execute(
                    select(F)
                    .where(F.c.uploaded_by_user_id == principal.user_id, F.c.request_id == rid)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        now = await db.scalar(select(func.clock_timestamp()))
        if old:
            if old["payload_sha256"] != digest:
                raise AccessError("scan_request_conflict", 409)
            if old["status"] == "stored":
                return dict(old), True
            if old["status"] == "receiving" and old["lease_expires_at"] > now:
                raise AccessError("scan_upload_in_progress", 409)
            identifier, attempt = old["id"], old["attempt_number"] + 1
        else:
            if await db.scalar(
                select(F.c.id).where(F.c.batch_id == bid, F.c.sha256 == staged.sha256)
            ):
                raise AccessError("scan_duplicate_file", 409)
            if (
                await db.scalar(select(func.count()).select_from(F).where(F.c.batch_id == bid))
                >= 20
            ):
                raise AccessError("scan_file_limit", 409)
            identifier, attempt = uuid4(), 1
        token = uuid4()
        prefix = f"{storage.object_key_prefix}/" if storage.object_key_prefix else ""
        key = (
            f"{prefix}exam-scans/{batch['exam_session_id']}/{bid}/{identifier}/{token}/original.pdf"
        )
        changes = dict(
            attempt_token=token,
            attempt_number=attempt,
            status="receiving",
            failure_code=None,
            page_count=None,
            bucket=storage.bucket,
            object_key=key,
            lease_expires_at=now + timedelta(seconds=180),
            updated_at=now,
        )
        if old:
            query = update(F).where(F.c.id == identifier).values(**changes).returning(F)
        else:
            query = (
                insert(F)
                .values(
                    id=identifier,
                    batch_id=bid,
                    uploaded_by_user_id=principal.user_id,
                    request_id=rid,
                    payload_sha256=digest,
                    original_filename=staged.filename,
                    size_bytes=staged.size_bytes,
                    sha256=staged.sha256,
                    **changes,
                )
                .returning(F)
            )
        value = dict((await db.execute(query)).mappings().one())
        await audit(
            db,
            principal.user_id,
            rid,
            "upload_begin",
            digest,
            dict(file_id=str(identifier)),
            attempt,
        )
        return value, False


async def fail_attempt(db, value, code):
    # Internal compensation only for this exact server-created attempt; never overwrite stored.
    try:
        async with transaction(db):
            result = await db.execute(
                update(F)
                .where(
                    F.c.id == value["id"],
                    F.c.attempt_token == value["attempt_token"],
                    F.c.status == "receiving",
                )
                .values(status="failed", failure_code=code, updated_at=func.clock_timestamp())
                .returning(F.c.id)
            )
            if result.scalar_one_or_none():
                await audit(
                    db,
                    value["uploaded_by_user_id"],
                    value["request_id"],
                    "upload_failed",
                    value["payload_sha256"],
                    dict(file_id=str(value["id"]), failure_code=code),
                    value["attempt_number"],
                )
    except (AccessError, SQLAlchemyError, OSError, TimeoutError):
        log.warning("Scan failure registration unavailable file_id=%s", value["id"])


async def complete(db, principal, value, pages):
    async with transaction(db):
        await http_authorize(db, principal, {"batch_id": value["batch_id"]}, "scan.file.upload")
        batch = await exams.row(db, B, value["batch_id"])
        await writable_session(db, batch["exam_session_id"])
        current = await exams.row(db, F, value["id"], lock=True)
        now = await db.scalar(select(func.clock_timestamp()))
        if (
            current["attempt_token"] != value["attempt_token"]
            or current["status"] != "receiving"
            or current["lease_expires_at"] <= now
        ):
            raise AccessError("scan_upload_lease_lost", 409)
        await db.execute(
            insert(P),
            [
                dict(
                    id=uuid5(value["id"], f"page:{p['page_index']}"),
                    file_id=value["id"],
                    object_key=value["object_key"].rsplit("/", 1)[0]
                    + f"/page-{p['page_index']}.jpg",
                    **p,
                )
                for p in pages
            ],
        )
        result = (
            (
                await db.execute(
                    update(F)
                    .where(F.c.id == value["id"])
                    .values(
                        status="stored", page_count=len(pages), failure_code=None, updated_at=now
                    )
                    .returning(F)
                )
            )
            .mappings()
            .one()
        )
        await audit(
            db,
            principal.user_id,
            value["request_id"],
            "upload_stored",
            value["payload_sha256"],
            dict(file_id=str(value["id"]), pages=len(pages)),
            value["attempt_number"],
        )
        return FileRead.model_validate(result)


async def upload(db, principal, storage, bid, rid, file: UploadFile):
    if not (file.filename or "").lower().endswith(".pdf") or file.content_type != "application/pdf":
        raise AccessError("scan_pdf_required", 415)
    value = None
    staged = None
    try:
        async with asyncio.timeout(settings.scan_upload_timeout_seconds):
            staged = await stage_upload(file, settings.scan_max_file_bytes)
            value, replay = await reserve(db, principal, storage, bid, rid, staged)
            if replay:
                return FileRead.model_validate(value), True
            with tempfile.TemporaryDirectory(prefix="huipi-scan-") as directory:
                root = Path(directory)
                with (root / "original.pdf").open("wb") as target:
                    await asyncio.to_thread(shutil.copyfileobj, staged.fileobj, target, 64 * 1024)
                pages = await previews(
                    root, settings.scan_max_pages, settings.scan_render_timeout_seconds
                )
                await storage.upload_path(
                    root / "original.pdf", value["object_key"], "application/pdf"
                )
                for page in pages:
                    filename = f"page-{page['page_index']}.jpg"
                    await storage.upload_path(
                        root / filename,
                        value["object_key"].rsplit("/", 1)[0] + "/" + filename,
                        "image/jpeg",
                    )
                return await complete(db, principal, value, pages), False
    except BaseException as error:
        if value is not None:
            code = (
                error.code
                if isinstance(error, AccessError)
                else (
                    "scan_storage_unavailable"
                    if isinstance(error, StorageUnavailableError)
                    else "scan_upload_interrupted"
                )
            )
            # Do not mutate a possibly committed result; matching receiving-only update fences it.
            if code != "scan_commit_outcome_unknown":
                await fail_attempt(db, value, code)
        if isinstance(error, TimeoutError):
            raise AccessError("scan_upload_timeout", 503) from None
        raise
    finally:
        if staged:
            await asyncio.to_thread(staged.fileobj.close)


async def download_metadata(db, principal, *, fid=None, pid=None):
    async with transaction(db):
        await http_authorize(
            db, principal, {"page_id": pid} if pid else {"file_id": fid}, "scan.file.read"
        )
        if pid:
            page = dict(await exams.row(db, P, pid))
            value = await exams.row(db, F, page["file_id"])
            page["bucket"] = value["bucket"]
        else:
            value = await exams.row(db, F, fid)
            page = dict(value)
        if value["status"] != "stored":
            raise AccessError("scan_file_not_ready", 409)
        return page


async def enable_operator(db, actor_id, target_id, request_id, approval_ref):
    """Explicit deployment-only bootstrap, never an HTTP or Agent tool."""
    from datetime import UTC, datetime

    from huipi_cloud.modules.identity.models import User
    from huipi_cloud.modules.identity.protocol import Principal
    from huipi_cloud.modules.resource_security import service as security

    from .permissions import SCAN_DEPLOYER_ROLE

    if not approval_ref.strip() or len(approval_ref) > 120:
        raise AccessError("scan_approval_required", 422)
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
    async with transaction(db):
        scope = await security.installation_scope(db)
        await security.require_permission(db, principal, "security.grants.manage", scope.id)
        await db.execute(
            select(
                func.pg_advisory_xact_lock(
                    int.from_bytes(
                        hashlib.sha256(f"scan:{actor_id}:{request_id}".encode()).digest()[:8],
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
                raise AccessError("scan_request_conflict", 409)
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
                role_version_id=SCAN_DEPLOYER_ROLE,
                scope_id=scope.id,
            )
        )
        result = dict(grant_id=str(identifier))
        await audit(db, actor_id, request_id, "enable_operator", digest, result)
        return result
