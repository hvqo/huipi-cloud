"""Dedicated protected backend for the existing bounded ParsingWorker lifecycle."""

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta
from io import BytesIO
from uuid import uuid4, uuid5

from sqlalchemy import func, insert, or_, select, update

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.answer_scripts import service as scripts
from huipi_cloud.modules.canonical_documents import service as canonical
from huipi_cloud.modules.canonical_documents.normalizer import (
    NormalizationSource,
    normalize_middle_json,
)
from huipi_cloud.modules.canonical_documents.protocol import (
    CANONICAL_SCHEMA_VERSION,
    NORMALIZER_VERSION,
)
from huipi_cloud.modules.identity.protocol import AccessError
from huipi_cloud.modules.parsing.enums import ParsingTaskStatus
from huipi_cloud.modules.parsing.errors import FailureSummary
from huipi_cloud.modules.parsing.executor import ParsingInput
from huipi_cloud.modules.parsing.repository import ClaimedTask
from huipi_cloud.modules.submissions.models import ParsingTask, Submission, SubmissionFile

from . import service
from .materialize import assemble

J, K = service.J, service.K
T, SUB, F = (m.__table__ for m in (ParsingTask, Submission, SubmissionFile))


@asynccontextmanager
async def worker_transaction(factory):
    async with factory() as db, db.begin():
        await db.execute(
            select(
                func.set_config("statement_timeout", "8000", True),
                func.set_config("lock_timeout", "5000", True),
            )
        )
        yield db


class ExamBackend:
    """Only fixed job IDs resolve protected context; no privileged generic Session exists."""

    def __init__(self, storage, config=settings):
        self.storage, self.config = storage, config
        self.prepared = None
        self.current_claim = None
        self.factory = None

    async def source(self, db, job):
        principal = await service.actor(db, job["requested_by_user_id"])
        return await service.context(
            db, principal, job["answer_script_id"], version_id=job["answer_script_version_id"]
        )

    async def leased(self, db, jid, token):
        job = (
            (await db.execute(select(J).where(J.c.id == jid).with_for_update(key_share=True)))
            .mappings()
            .first()
        )
        now = await db.scalar(select(func.clock_timestamp()))  # after lock acquisition
        if (
            job is None
            or job["status"] != "running"
            or job["lease_token"] != token
            or job["lease_expires_at"] <= now
        ):
            return None
        return job

    async def invalidate(self, db, job):
        await db.execute(
            update(J)
            .where(J.c.id == job["id"])
            .values(
                status="invalidated",
                failure_code="source_or_authority_changed",
                lease_token=None,
                lease_expires_at=None,
                next_run_at=None,
                updated_at=func.clock_timestamp(),
            )
        )
        await service.audit(db, job, "invalidated")

    async def valid(self, db, job):
        try:
            return await self.source(db, job)
        except AccessError as error:
            if error.status_code not in {401, 403, 404, 409}:
                raise
            await self.invalidate(db, job)
            return None

    async def claim_next_task(self, factory, *, lease_seconds):
        self.factory = factory
        self.current_claim = None
        if lease_seconds < 120:
            raise ValueError("exam parsing lease must cover bounded input assembly (>=120s)")
        async with worker_transaction(factory) as db:
            await db.execute(select(func.set_config("lock_timeout", "5000", True)))
            eligible = or_(
                J.c.status == "pending",
                (J.c.status == "retry_wait") & (J.c.next_run_at <= func.clock_timestamp()),
                (J.c.status == "running") & (J.c.lease_expires_at <= func.clock_timestamp()),
            )
            job = (
                (
                    await db.execute(
                        select(J)
                        .where(eligible)
                        .order_by(J.c.created_at, J.c.id)
                        .limit(1)
                        .with_for_update(skip_locked=True, key_share=True)
                    )
                )
                .mappings()
                .first()
            )
            if job is None:
                return None
            if job["attempt_count"] >= 3:
                await db.execute(
                    update(J)
                    .where(J.c.id == job["id"])
                    .values(
                        status="failed",
                        failure_code="attempts_exhausted",
                        lease_token=None,
                        lease_expires_at=None,
                        next_run_at=None,
                    )
                )
                await service.audit(db, job, "exhausted")
                return None
            source = await self.valid(db, job)
            if source is None:
                return None
            token = uuid4()
            job = (
                (
                    await db.execute(
                        update(J)
                        .where(J.c.id == job["id"])
                        .values(
                            status="running",
                            attempt_count=job["attempt_count"] + 1,
                            lease_token=token,
                            lease_expires_at=func.clock_timestamp()
                            + timedelta(seconds=lease_seconds),
                            next_run_at=None,
                            failure_code=None,
                            updated_at=func.clock_timestamp(),
                        )
                        .returning(J)
                    )
                )
                .mappings()
                .one()
            )
            await service.audit(db, job, "claim")
        try:
            task = await self.materialize(factory, job, source, lease_seconds)
        except (AccessError, StorageUnavailableError, OSError, TimeoutError):
            await self.record_failure(
                factory,
                task_id=job["id"],
                lease_token=token,
                failure=FailureSummary("input_materialization_failed", "答卷来源暂不可用", True),
                retry_delay_seconds=15,
            )
            return None
        if task is None:
            return None
        self.current_claim = (job["id"], token)
        return ClaimedTask(task, token, job["lease_expires_at"])

    async def materialize(self, factory, job, source, lease_seconds):
        async with factory() as db:
            link = (await db.execute(select(K).where(K.c.job_id == job["id"]))).mappings().first()
        if not link:
            _, version, rows, sp, pv = source
            key = (
                f"{self.storage.object_key_prefix}/exam-input/{job['id']}/"
                f"{job['lease_token']}/answer.pdf"
            )
            value = await assemble(self.storage, rows, key)
            async with worker_transaction(factory) as db:
                current = await self.leased(db, job["id"], job["lease_token"])
                if current is None or await self.valid(db, current) is None:
                    return None
                # Source digest/version is checked again after assembly/upload, before any
                # protected Submission/File is materialized. ACK loss never deletes objects.
                sid, fid = uuid4(), uuid4()
                await db.execute(
                    insert(SUB).values(
                        id=sid,
                        assignment_id=pv["canonical_assignment_id"],
                        student_ref=str(version["id"]),
                    )
                )
                await db.execute(
                    insert(F).values(
                        id=fid,
                        submission_id=sid,
                        original_filename="confirmed-answer.pdf",
                        content_type="application/pdf",
                        **value,
                    )
                )
                await db.execute(
                    insert(T).values(
                        id=job["id"], submission_id=sid, status="pending", max_attempts=3
                    )
                )
                link = dict(
                    job_id=job["id"],
                    answer_script_id=job["answer_script_id"],
                    answer_script_version_id=version["id"],
                    exam_session_id=job["exam_session_id"],
                    roster_revision_id=version["roster_revision_id"],
                    roster_entry_id=version["roster_entry_id"],
                    participation_id=version["participation_id"],
                    session_paper_id=sp["id"],
                    paper_version_id=pv["id"],
                    submission_id=sid,
                    submission_file_id=fid,
                    parsing_task_id=job["id"],
                    manifest_sha256=version["manifest_sha256"],
                )
                await db.execute(insert(K).values(**link))
                await service.audit(db, job, "materialized")
        async with worker_transaction(factory) as db:
            current = await self.leased(db, job["id"], job["lease_token"])
            if current is None or await self.valid(db, current) is None:
                return None
            value = await scripts.exams.row(db, F, link["submission_file_id"])
            await db.execute(
                update(J)
                .where(J.c.id == job["id"])
                .values(lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds))
            )
            return ParsingInput(
                job["id"],
                link["submission_id"],
                value["bucket"],
                value["object_key"],
                value["content_type"],
                value["size_bytes"],
                value["sha256"],
                job["attempt_count"],
            )

    async def heartbeat(self, factory, *, task_id, lease_token, lease_seconds):
        async with worker_transaction(factory) as db:
            job = await self.leased(db, task_id, lease_token)
            if job is None or await self.valid(db, job) is None:
                return None
            return await db.scalar(
                update(J)
                .where(J.c.id == task_id)
                .values(
                    lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds),
                    updated_at=func.clock_timestamp(),
                )
                .returning(J.c.lease_expires_at)
            )

    async def record_failure(self, factory, *, task_id, lease_token, failure, retry_delay_seconds):
        async with worker_transaction(factory) as db:
            job = await self.leased(db, task_id, lease_token)
            if job is None or await self.valid(db, job) is None:
                return None
            retry = failure.retryable and job["attempt_count"] < 3
            state = "retry_wait" if retry else "failed"
            await db.execute(
                update(J)
                .where(J.c.id == task_id)
                .values(
                    status=state,
                    lease_token=None,
                    lease_expires_at=None,
                    failure_code=failure.code[:64],
                    next_run_at=func.clock_timestamp() + timedelta(seconds=retry_delay_seconds)
                    if retry
                    else None,
                    updated_at=func.clock_timestamp(),
                )
            )
            await service.audit(db, job, state)
            return ParsingTaskStatus(state)

    async def verify_execution_sources(self, task):
        if self.current_claim is None or self.current_claim[0] != task.task_id:
            raise ValueError("executor is not bound to this claimed examination task")
        async with worker_transaction(self.factory) as db:
            job = await self.leased(db, task.task_id, self.current_claim[1])
            if job is None:
                raise AccessError("exam_parse_lease_lost", 409)
            source = await self.source(db, job)
        await scripts.verify_sources(self.storage, source[2])

    async def complete_task_with_artifact(self, factory, *, artifact, lease_token):
        prepared = self.prepared
        self.prepared = None
        if prepared is None or prepared[0] != artifact.artifact_id:
            raise ValueError("canonical result must be prepared by the protected executor")
        async with worker_transaction(factory) as db:
            job = await self.leased(db, artifact.task_id, lease_token)
            if job is None:
                return False
            source = await self.valid(db, job)
            if source is None:
                return False
            link = await db.scalar(select(K.c.submission_id).where(K.c.job_id == job["id"]))
            file = (await db.execute(select(F).where(F.c.submission_id == link))).mappings().one()
            if (
                artifact.submission_id != link
                or artifact.bucket != file["bucket"]
                or artifact.original_sha256 != file["sha256"]
                or artifact.page_count != len(source[2])
            ):
                raise ValueError("parser result does not match the pinned protected input")
            values = asdict(artifact)
            values["id"] = values.pop("artifact_id")
            values["parsing_task_id"] = values.pop("task_id")
            await db.execute(insert(service.PA).values(**values))
            canonical_values = prepared[1]
            await db.execute(insert(service.CA).values(**canonical_values))
            await db.execute(
                update(T)
                .where(T.c.id == job["id"])
                .values(
                    status="succeeded",
                    finished_at=func.clock_timestamp(),
                    lease_token=None,
                    lease_expires_at=None,
                )
            )
            await db.execute(
                update(J)
                .where(J.c.id == job["id"])
                .values(
                    status="succeeded",
                    lease_token=None,
                    lease_expires_at=None,
                    parsed_artifact_id=artifact.artifact_id,
                    canonical_artifact_id=canonical_values["id"],
                    failure_code=None,
                    updated_at=func.clock_timestamp(),
                )
            )
            await service.audit(db, job, "succeeded")
            return True


class ProtectedExecutor:
    """Reuse MinerU plus the exact existing Canonical normalizer, within worker heartbeats."""

    def __init__(self, parser, backend):
        self.parser, self.backend = parser, backend

    async def execute(self, task):
        await self.backend.verify_execution_sources(task)
        artifact = await self.parser.execute(task)
        cfg, storage = self.backend.config, self.backend.storage
        if (
            artifact.task_id != task.task_id
            or artifact.submission_id != task.submission_id
            or artifact.original_sha256 != task.sha256
            or artifact.bucket != task.bucket
        ):
            raise ValueError("invalid protected parser result identity")
        middle = await canonical._read_checked_object(
            storage,
            artifact.middle_json_key,
            expected_size=artifact.middle_json_size_bytes,
            expected_sha256=artifact.middle_json_sha256,
            maximum=cfg.canonical_max_middle_json_bytes,
        )
        manifest = await canonical._read_checked_object(
            storage,
            artifact.assets_manifest_key,
            expected_size=artifact.assets_manifest_size_bytes,
            expected_sha256=artifact.assets_manifest_sha256,
            maximum=cfg.canonical_max_manifest_bytes,
        )
        did = uuid5(artifact.artifact_id, f"{CANONICAL_SCHEMA_VERSION}:{NORMALIZER_VERSION}")
        source = NormalizationSource(
            did,
            task.submission_id,
            artifact.artifact_id,
            artifact.parser_name,
            artifact.parser_version,
            artifact.schema_name,
            artifact.schema_version,
            artifact.original_sha256,
            artifact.middle_json_sha256,
            artifact.page_count,
            artifact.asset_count,
            artifact.tier,
        )
        document = normalize_middle_json(
            middle.data,
            manifest.data,
            source,
            max_nodes=cfg.canonical_max_nodes,
            max_nesting_depth=cfg.canonical_max_nesting_depth,
        )
        await canonical._verify_referenced_assets(
            storage, document, canonical._asset_object_keys(manifest.data, document)
        )
        body = json.dumps(
            document.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(body) > cfg.canonical_max_document_bytes:
            raise ValueError("canonical size limit")
        key = f"{storage.object_key_prefix}/exam-canonical/{task.task_id}/{uuid4()}.json"
        await storage.upload_fileobj(BytesIO(body), key, "application/json")
        sha = hashlib.sha256(body).hexdigest()
        await canonical._read_checked_object(
            storage,
            key,
            expected_size=len(body),
            expected_sha256=sha,
            maximum=cfg.canonical_max_document_bytes,
        )
        await self.backend.verify_execution_sources(task)
        self.backend.prepared = (
            artifact.artifact_id,
            dict(
                id=did,
                parsed_artifact_id=artifact.artifact_id,
                submission_id=task.submission_id,
                bucket=storage.bucket,
                schema_version=CANONICAL_SCHEMA_VERSION,
                normalizer_version=NORMALIZER_VERSION,
                status="available",
                canonical_object_key=key,
                canonical_sha256=sha,
                canonical_size_bytes=len(body),
                page_count=document.page_count,
                block_count=sum(len(p.blocks) for p in document.pages),
                source_parser=artifact.parser_name,
                source_parser_version=artifact.parser_version,
                source_sha256=task.sha256,
            ),
        )
        return artifact

    async def aclose(self):
        from huipi_cloud.modules.parsing.executor import close_executor

        await close_executor(self.parser)
