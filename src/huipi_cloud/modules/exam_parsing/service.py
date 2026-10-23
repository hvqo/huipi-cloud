"""Fixed resource operations; no caller-selected table, scope, storage key or identity."""

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, insert, select

from huipi_cloud.infrastructure.storage.s3 import (
    StorageObjectNotFoundError,
    StorageUnavailableError,
)
from huipi_cloud.modules.answer_scripts import service as scripts
from huipi_cloud.modules.canonical_documents.errors import CanonicalNormalizationError
from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact
from huipi_cloud.modules.canonical_documents.protocol import CanonicalDocument
from huipi_cloud.modules.canonical_documents.service import _read_checked_object
from huipi_cloud.modules.identity.models import User
from huipi_cloud.modules.identity.protocol import AccessError, Principal
from huipi_cloud.modules.parsing.models import ParsedArtifact
from huipi_cloud.modules.scans import service as scans

from .models import ExamParsingJob, ExamParsingOperation, ExamSubmissionLink

J, OPS, K = (m.__table__ for m in (ExamParsingJob, ExamParsingOperation, ExamSubmissionLink))
PA, CA = ParsedArtifact.__table__, CanonicalArtifact.__table__


async def context(db, principal, aid, *, version_id=None, action="exam.parse.start", lock=True):
    root = await scripts.exams.row(db, scripts.A, aid)
    sid = root["exam_session_id"]
    await scripts.authorize(db, principal, sid, action, "exam.script.read", "exam.identity.verify")
    await scripts.authorize(db, principal, sid, "exam.roster.read")
    if lock:
        await scans.writable_session(db, sid)
        root = await scripts.exams.row(db, scripts.A, aid, share=True)
    if version_id is not None and root["current_version_id"] != version_id:
        raise AccessError("exam_parse_source_changed", 409)
    version = await scripts.exams.row(db, scripts.V, root["current_version_id"])
    if await scripts.readiness(db, root, version) != "identity_confirmed":
        raise AccessError("exam_parse_source_not_ready", 409)
    sp = await scripts.exams.row(db, scripts.SP, version["session_paper_id"], share=lock)
    pv = await scripts.exams.row(db, scripts.PV, sp["paper_version_id"], share=lock)
    if sp["status"] != "active" or pv["status"] != "locked":
        raise AccessError("exam_parse_paper_unavailable", 409)
    ids = list(
        (
            await db.scalars(
                select(scripts.L.c.scan_page_id)
                .where(scripts.L.c.version_id == version["id"])
                .order_by(scripts.L.c.ordinal)
            )
        ).all()
    )
    rows = await scripts.source_rows(db, sid, ids)
    scripts.verify_manifest(version, rows)
    return root, version, rows, sp, pv


async def actor(db, user_id):
    # A job records a server-authenticated intent, not a reusable JWT. Every phase checks
    # the current account/grants; the worker's short-lived Principal never leaves this module.
    user = await db.get(User, user_id, populate_existing=True)
    if user is None or not user.is_active:
        raise AccessError("exam_parse_authority_revoked", 404)
    return Principal(
        user_id=user.id,
        issuer=user.issuer,
        subject=user.subject,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


async def audit(db, job, action, *, request_id=None, digest=None, actor_id=None):
    await db.execute(
        insert(OPS).values(
            id=uuid4(),
            job_id=job["id"],
            actor_user_id=actor_id or job["requested_by_user_id"],
            request_id=request_id or uuid4(),
            action=action,
            payload_sha256=digest
            or scripts.exams.digest(
                dict(job=str(job["id"]), action=action, attempt=job["attempt_count"])
            ),
            result=dict(job_id=str(job["id"])),
        )
    )


async def start(db, principal, storage, aid, request):
    # Validates real immutable source bytes, current identity and the persisted digest.
    async with scans.transaction(db):
        await context(db, principal, aid)
    manifest = await scripts.manifest(db, principal, storage, aid)
    digest = scripts.exams.digest(dict(script=str(aid), body=request.model_dump(mode="json")))
    async with scans.transaction(db):
        root, version, _, _, _ = await context(
            db, principal, aid, version_id=manifest["answer_script_version_id"]
        )
        if root["revision"] != request.expected_revision:
            raise AccessError("script_revision_conflict", 409)
        await db.execute(
            select(
                func.pg_advisory_xact_lock(
                    int.from_bytes(
                        hashlib.sha256(
                            f"exam-parse:{principal.user_id}:{request.request_id}".encode()
                        ).digest()[:8],
                        signed=True,
                    )
                )
            )
        )
        previous = (
            (
                await db.execute(
                    select(OPS).where(
                        OPS.c.actor_user_id == principal.user_id,
                        OPS.c.request_id == request.request_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if previous:
            if previous["payload_sha256"] != digest:
                raise AccessError("exam_parse_request_conflict", 409)
            return {"job_id": previous["job_id"]}
        job = (
            (await db.execute(select(J).where(J.c.answer_script_version_id == version["id"])))
            .mappings()
            .first()
        )
        if not job:
            values = dict(
                id=uuid4(),
                answer_script_id=aid,
                answer_script_version_id=version["id"],
                exam_session_id=root["exam_session_id"],
                requested_by_user_id=principal.user_id,
            )
            job = (await db.execute(insert(J).values(**values).returning(J))).mappings().one()
        await audit(
            db,
            job,
            "request",
            request_id=request.request_id,
            digest=digest,
            actor_id=principal.user_id,
        )
        return {"job_id": job["id"]}


async def http_authorize(db, principal, params, action):
    if params.get("script_id"):
        aid = UUID(str(params["script_id"]))
    else:
        job = await scripts.exams.row(db, J, UUID(str(params["job_id"])))
        aid = job["answer_script_id"]
    root = await scripts.exams.row(db, scripts.A, aid)
    await scripts.authorize(db, principal, root["exam_session_id"], action)


async def status(db, principal, *, aid=None, jid=None):
    async with scans.transaction(db):
        if jid:
            job = await scripts.exams.row(db, J, jid)
            aid = job["answer_script_id"]
        root = await scripts.exams.row(db, scripts.A, aid)
        await scripts.authorize(db, principal, root["exam_session_id"], "exam.parse.read")
        await scripts.authorize(db, principal, root["exam_session_id"], "exam.roster.read")
        if not jid:
            job = (
                (
                    await db.execute(
                        select(J).where(J.c.answer_script_version_id == root["current_version_id"])
                    )
                )
                .mappings()
                .first()
            )
        if not job:
            return dict(status="not_started", current=True, page_count=None, job_id=None)
        valid, reason = True, None
        try:
            await context(
                db,
                await actor(db, job["requested_by_user_id"]),
                aid,
                version_id=job["answer_script_version_id"],
                lock=True,
            )
        except AccessError as error:
            if error.status_code not in {401, 403, 404, 409}:
                raise
            valid, reason = False, "source_or_authority_changed"
        link = (await db.execute(select(K).where(K.c.job_id == job["id"]))).mappings().first()
        canonical = (
            await scripts.exams.row(db, CA, job["canonical_artifact_id"])
            if (valid and job["status"] == "succeeded")
            else None
        )
        return dict(
            job_id=job["id"],
            status=job["status"] if valid else "invalidated",
            current=valid,
            failure_code=reason or job["failure_code"],
            answer_script_version_id=job["answer_script_version_id"],
            page_count=canonical["page_count"] if canonical else None,
            block_count=canonical["block_count"] if canonical else None,
            canonical_artifact_id=canonical["id"] if canonical else None,
            parsed_artifact_id=job["parsed_artifact_id"] if canonical else None,
            manifest_sha256=link["manifest_sha256"] if link else None,
            attempt_count=job["attempt_count"],
            **{
                name: link[name] if valid and link else None
                for name in (
                    "roster_revision_id",
                    "roster_entry_id",
                    "participation_id",
                    "session_paper_id",
                    "paper_version_id",
                )
            },
        )


async def page(db, principal, storage, jid, number, config):
    async with scans.transaction(db):
        job = await scripts.exams.row(db, J, jid)
        await context(
            db,
            principal,
            job["answer_script_id"],
            version_id=job["answer_script_version_id"],
            action="exam.parse.read",
        )
        await context(
            db,
            await actor(db, job["requested_by_user_id"]),
            job["answer_script_id"],
            version_id=job["answer_script_version_id"],
        )
        if job["status"] != "succeeded":
            raise AccessError("exam_parse_not_available", 409)
        canonical = await scripts.exams.row(db, CA, job["canonical_artifact_id"])
        if canonical["bucket"] != storage.bucket:
            raise AccessError("exam_parse_storage_unavailable", 503)
    try:
        async with asyncio.timeout(60):
            payload = await _read_checked_object(
                storage,
                canonical["canonical_object_key"],
                expected_size=canonical["canonical_size_bytes"],
                expected_sha256=canonical["canonical_sha256"],
                maximum=config.canonical_max_document_bytes,
            )
            document = CanonicalDocument.model_validate_json(payload.data)
    except (
        CanonicalNormalizationError,
        ValidationError,
        StorageUnavailableError,
        StorageObjectNotFoundError,
        TimeoutError,
    ):
        raise AccessError("exam_parse_result_unavailable", 503) from None
    if (
        document.document_id != canonical["id"]
        or document.source_artifact_id != canonical["parsed_artifact_id"]
        or document.submission_id != canonical["submission_id"]
        or document.source_sha256 != canonical["source_sha256"]
        or document.page_count != canonical["page_count"]
    ):
        raise AccessError("exam_parse_result_integrity_failed", 503)
    if not 0 <= number < document.page_count:
        raise AccessError("exam_parse_page_not_found", 404)
    if len(document.pages[number].model_dump_json().encode()) > 1024 * 1024:
        raise AccessError("exam_parse_page_limit", 413)
    async with scans.transaction(db):
        _, _, rows, _, _ = await context(
            db,
            principal,
            job["answer_script_id"],
            version_id=job["answer_script_version_id"],
            action="exam.parse.read",
        )
        await context(
            db,
            await actor(db, job["requested_by_user_id"]),
            job["answer_script_id"],
            version_id=job["answer_script_version_id"],
        )
    return dict(
        answer_script_version_id=job["answer_script_version_id"],
        canonical_artifact_id=canonical["id"],
        page=document.pages[number],
        source=scripts.public_page(rows[number]),
    )


async def enable_operator(db, actor_id, target_id, request_id, approval_ref):
    """Deployment-only fixed-role bootstrap, with current authority and same-tx audit."""
    from huipi_cloud.modules.resource_security import service as security
    from huipi_cloud.modules.resource_security.models import SecurityGrant

    from .permissions import PARSE_DEPLOYER_ROLE

    if not approval_ref.strip() or len(approval_ref) > 120:
        raise AccessError("exam_parse_approval_required", 422)
    principal = await actor(db, actor_id)
    await db.rollback()
    async with security.operation(
        db,
        principal,
        request_id,
        "bootstrap",
        dict(target=str(target_id), approval_ref=approval_ref, role="exam_parse_deployer"),
    ) as op:
        scope = await security.installation_scope(db)
        await security.require_permission(db, principal, "security.grants.manage", scope.id)
        if op.replay is not None:
            return op.replay
        user = await db.scalar(
            select(User).where(User.id == target_id, User.is_active).with_for_update(read=True)
        )
        if user is None:
            raise AccessError(scripts.DENIED, 404)
        gid = uuid4()
        await db.execute(
            insert(SecurityGrant).values(
                id=gid, user_id=target_id, role_version_id=PARSE_DEPLOYER_ROLE, scope_id=scope.id
            )
        )
        op.result = dict(grant_id=str(gid))
        return op.result
