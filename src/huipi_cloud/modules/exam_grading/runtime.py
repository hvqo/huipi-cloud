"""Single-call, leased exam scoring. No task receives a generic privileged session."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, insert, select, update

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.canonical_documents.errors import CanonicalNormalizationError
from huipi_cloud.modules.exam_parsing.runtime import worker_transaction
from huipi_cloud.modules.grading import proposal_service as proposals
from huipi_cloud.modules.grading.proposal_errors import GradeProposalError
from huipi_cloud.modules.grading.proposal_protocol import GradeProposalDocument
from huipi_cloud.modules.grading.response_audit import response_auditor
from huipi_cloud.modules.identity.protocol import AccessError

from . import service as s


class ScoringWorker:
    def __init__(self, factory, storage, provider, config=settings):
        self.factory, self.storage, self.provider = factory, storage, provider
        self.config = config.model_copy(update={"grading_invalid_response_retries": 0})

    async def valid(self, db, job):
        p = await s.parsing.actor(db, job["requested_by_user_id"])
        c, indexed = await s.scoring_context(db, p, job, "exam.score.request", self.config)
        return p, c, indexed

    async def leased(self, db, jid, token):
        job = (
            (await db.execute(select(s.S).where(s.S.c.id == jid).with_for_update(key_share=True)))
            .mappings()
            .one()
        )
        now = await db.scalar(select(func.clock_timestamp()))
        return (
            job
            if job["status"] == "running"
            and job["lease_token"] == token
            and job["lease_expires_at"] > now
            else None
        )

    async def transition(self, db, job, state, code=None):
        await db.execute(
            update(s.S)
            .where(s.S.c.id == job["id"])
            .values(status=state, lease_token=None, lease_expires_at=None, failure_code=code)
        )
        await s.audit(
            db,
            s.NS(user_id=job["requested_by_user_id"]),
            job["parsing_job_id"]
            if "parsing_job_id" in job
            else (await s.row(db, s.W, job["workspace_id"]))["parsing_job_id"],
            uuid4(),
            state,
            s.json_sha256(dict(job=str(job["id"]), state=state)),
            dict(scoring_job_id=str(job["id"])),
        )

    async def claim(self):
        async with worker_transaction(self.factory) as db:
            expired = (
                (
                    await db.execute(
                        select(s.S)
                        .where(
                            s.S.c.status == "running",
                            s.S.c.lease_expires_at <= func.clock_timestamp(),
                        )
                        .limit(1)
                        .with_for_update(skip_locked=True, key_share=True)
                    )
                )
                .mappings()
                .first()
            )
            if expired:
                await self.transition(db, expired, "failed", "execution_outcome_unknown")
                return None
            job = (
                (
                    await db.execute(
                        select(s.S)
                        .where(s.S.c.status == "pending")
                        .order_by(s.S.c.created_at, s.S.c.id)
                        .limit(1)
                        .with_for_update(skip_locked=True, key_share=True)
                    )
                )
                .mappings()
                .first()
            )
            if not job:
                return None
            try:
                await self.valid(db, job)
            except AccessError:
                await self.transition(db, job, "invalidated", "source_or_authority_changed")
                return None
            token = uuid4()
            return (
                (
                    await db.execute(
                        update(s.S)
                        .where(s.S.c.id == job["id"])
                        .values(
                            status="running",
                            lease_token=token,
                            lease_expires_at=func.clock_timestamp() + timedelta(seconds=180),
                        )
                        .returning(s.S)
                    )
                )
                .mappings()
                .one()
            )

    async def heartbeat(self, job):
        async with worker_transaction(self.factory) as db:
            current = await self.leased(db, job["id"], job["lease_token"])
            if not current:
                return False
            try:
                await self.valid(db, current)
            except AccessError:
                await self.transition(db, current, "invalidated", "source_or_authority_changed")
                return False
            await db.execute(
                update(s.S)
                .where(s.S.c.id == job["id"])
                .values(lease_expires_at=func.clock_timestamp() + timedelta(seconds=180))
            )
            return True

    async def generate(self, job):
        async with worker_transaction(self.factory) as db:
            p, c, indexed = await self.valid(db, job)
        async with self.factory() as db:
            c = await s.bundle(
                db, p, self.storage, job["workspace_id"], "exam.score.request", self.config
            )
        await s.parsing.scripts.verify_sources(self.storage, c.source[2])
        document = await s.read_input(self.storage, indexed, self.config)
        if job["visual_selection_id"] is not None:
            from . import visual_scoring, visual_selection

            images = await visual_selection.materialize(self.storage, document.regions, c.source[2])
            proposal = await visual_scoring.make_proposal(
                self.provider, document, images, job["id"], self.config
            )
        else:
            prompt_version = proposals.production_prompt_version(self.config)
            reply, model, usage, elapsed = await proposals._call_and_validate(
                self.provider,
                document,
                config=self.config,
                prompt_version=prompt_version,
                audit_response=(
                    response_auditor(
                        self.storage, document, job["id"], self.provider, prompt_version
                    )
                    if self.config.grading_strategy == "quote_selection"
                    else None
                ),
            )
            if reply.outcome == "abstain":
                scores, total, state = [], None, "grading_abstained"
            else:
                scores, total = proposals._validate_candidate_scores(reply, document, self.config)
                state = "completed_candidate"
            proposal = GradeProposalDocument(
                proposal_id=uuid4(),
                request_id=job["id"],
                request_fingerprint=proposals._request_fingerprint(
                    document, self.provider, prompt_version=prompt_version
                ),
                grade_input_artifact_id=document.artifact_id,
                grade_input_sha256=document.input_sha256,
                submission_id=document.submission_id,
                question_id=document.question.question_id,
                model_id=model,
                provider=self.provider.provider_name,
                prompt_version=prompt_version,
                generation_config_digest=self.provider.generation_config_digest,
                criterion_scores=scores,
                total_score=total,
                max_score=document.question.max_score,
                grading_status=state,
                reason_codes=reply.reason_codes,
                abstention_reason=reply.abstention_reason,
                prompt_tokens=usage[0],
                completion_tokens=usage[1],
                model_elapsed_ms=elapsed,
                created_at=datetime.now(UTC),
            )
        await s.parsing.scripts.verify_sources(self.storage, c.source[2])
        async with worker_transaction(self.factory) as db:
            await self.valid(db, job)
        stored = await s.upload(
            self.storage,
            "proposal",
            proposal.proposal_id,
            proposal,
            self.config.grading_max_proposal_bytes,
        )
        async with worker_transaction(self.factory) as db:
            current = await self.leased(db, job["id"], job["lease_token"])
            if not current:
                return False
            p, c, indexed = await self.valid(db, current)
            values = proposal.model_dump(
                exclude={
                    "schema_name",
                    "schema_version",
                    "criterion_scores",
                    "total_score",
                    "max_score",
                    "reason_codes",
                    "abstention_reason",
                },
                mode="python",
            )
            values["id"] = values.pop("proposal_id")
            values.update(**stored)
            await db.execute(insert(s.GP).values(**values))
            await db.execute(
                update(s.S)
                .where(s.S.c.id == job["id"])
                .values(
                    status="succeeded",
                    proposal_id=proposal.proposal_id,
                    lease_token=None,
                    lease_expires_at=None,
                )
            )
            await s.audit(
                db,
                p,
                c.job["id"],
                uuid4(),
                "candidate",
                s.json_sha256(dict(proposal=str(proposal.proposal_id))),
                dict(proposal_id=str(proposal.proposal_id)),
            )
        return True

    async def once(self):
        job = await self.claim()
        if not job:
            return False
        task = asyncio.create_task(self.generate(job))
        try:
            async with asyncio.timeout(180):
                while True:
                    done, _ = await asyncio.wait({task}, timeout=5)
                    if task in done:
                        await task
                        return True
                    if not await self.heartbeat(job):
                        return True
        except (
            AccessError,
            GradeProposalError,
            TimeoutError,
            StorageUnavailableError,
            CanonicalNormalizationError,
            ValidationError,
            OSError,
        ) as error:
            async with worker_transaction(self.factory) as db:
                current = await self.leased(db, job["id"], job["lease_token"])
                if current:
                    await self.transition(
                        db,
                        current,
                        "invalidated" if isinstance(error, AccessError) else "failed",
                        getattr(error, "reason_code", "source_or_authority_changed")[:64],
                    )
            return True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run(self, stop):
        while not stop.is_set():
            if not await self.once():
                try:
                    await asyncio.wait_for(stop.wait(), 1)
                except TimeoutError:
                    pass
