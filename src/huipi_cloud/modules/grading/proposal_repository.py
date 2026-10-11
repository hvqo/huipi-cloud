"""Short PostgreSQL transactions for source-checked rubric proposals."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.modules.grading import repository as grade_input_repository
from huipi_cloud.modules.grading.errors import GradeInputNotFoundError, GradeInputSourceChangedError
from huipi_cloud.modules.grading.models import GradeInputArtifact, GradeProposalArtifact
from huipi_cloud.modules.grading.proposal_errors import (
    GradeProposalCommitOutcomeUnknownError,
    GradeProposalIdempotencyConflictError,
    GradeProposalInputStaleError,
    GradeProposalNotFoundError,
    GradeProposalRegistrationRolledBackError,
)
from huipi_cloud.modules.grading.proposal_protocol import GradeProposalDocument
from huipi_cloud.modules.grading.protocol import (
    GradeInputCore,
    GradeInputDocument,
    json_sha256,
)

SessionFactory = async_sessionmaker[AsyncSession]
logger = logging.getLogger(__name__)


async def get_by_request_id(
    session_factory: SessionFactory,
    request_id: UUID,
) -> GradeProposalArtifact | None:
    async with session_factory() as session:
        return await session.scalar(
            select(GradeProposalArtifact).where(GradeProposalArtifact.request_id == request_id)
        )


async def get_proposal(
    session: AsyncSession,
    proposal_id: UUID,
) -> GradeProposalArtifact | None:
    return await session.scalar(
        select(GradeProposalArtifact).where(GradeProposalArtifact.id == proposal_id)
    )


async def register_proposal(
    session_factory: SessionFactory,
    *,
    input_document: GradeInputDocument,
    proposal: GradeProposalDocument,
    bucket: str,
    object_key: str,
    object_sha256: str,
    object_size_bytes: int,
) -> tuple[GradeProposalArtifact, bool]:
    """Lock and revalidate GradeInput sources before one immutable index insert."""

    commit_attempted = False
    try:
        async with session_factory() as session:
            transaction = await session.begin()
            try:
                indexed_input = await session.scalar(
                    select(GradeInputArtifact)
                    .where(GradeInputArtifact.id == input_document.artifact_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if indexed_input is None:
                    raise GradeProposalNotFoundError("grade_input_not_found")
                if (
                    indexed_input.input_sha256 != input_document.input_sha256
                    or indexed_input.submission_id != input_document.submission_id
                    or indexed_input.question_id != input_document.question.question_id
                    or json_sha256(
                        GradeInputCore.model_validate(
                            input_document.model_dump(
                                exclude={"artifact_id", "input_sha256", "created_at"}
                            )
                        )
                    )
                    != indexed_input.input_sha256
                ):
                    raise GradeProposalInputStaleError("grade_input_index_changed")

                core = GradeInputCore.model_validate(
                    input_document.model_dump(
                        exclude={"artifact_id", "input_sha256", "created_at"}
                    )
                )
                await grade_input_repository.lock_and_validate_sources(session, core)

                existing = await session.scalar(
                    select(GradeProposalArtifact).where(
                        GradeProposalArtifact.request_id == proposal.request_id
                    )
                )
                if existing is not None:
                    _check_same_request(existing, proposal)
                    await transaction.commit()
                    return existing, False

                statement = (
                    insert(GradeProposalArtifact)
                    .values(
                        id=proposal.proposal_id,
                        request_id=proposal.request_id,
                        grade_input_artifact_id=proposal.grade_input_artifact_id,
                        grade_input_sha256=proposal.grade_input_sha256,
                        submission_id=proposal.submission_id,
                        question_id=proposal.question_id,
                        provider=proposal.provider,
                        model_id=proposal.model_id,
                        prompt_version=proposal.prompt_version,
                        generation_config_digest=proposal.generation_config_digest,
                        request_fingerprint=proposal.request_fingerprint,
                        grading_status=proposal.grading_status,
                        bucket=bucket,
                        object_key=object_key,
                        object_sha256=object_sha256,
                        object_size_bytes=object_size_bytes,
                        prompt_tokens=proposal.prompt_tokens,
                        completion_tokens=proposal.completion_tokens,
                        model_elapsed_ms=proposal.model_elapsed_ms,
                    )
                    .on_conflict_do_nothing(constraint="uq_grade_proposals_request_id")
                )
                insert_result = await session.execute(statement)
                record = await session.scalar(
                    select(GradeProposalArtifact).where(
                        GradeProposalArtifact.request_id == proposal.request_id
                    )
                )
                if record is None:
                    raise RuntimeError("proposal registration did not produce an index")
                _check_same_request(record, proposal)
            except Exception as error:
                await _rollback_before_commit(transaction)
                if isinstance(error, GradeInputNotFoundError):
                    raise GradeProposalNotFoundError("grade_input_source_missing") from error
                if isinstance(error, GradeInputSourceChangedError):
                    raise GradeProposalInputStaleError(error.reason_code) from error
                raise

            commit_attempted = True
            await transaction.commit()
            return record, insert_result.rowcount == 1
    except Exception as error:
        if commit_attempted:
            raise GradeProposalCommitOutcomeUnknownError from error
        if isinstance(
            error,
            (
                GradeProposalNotFoundError,
                GradeProposalInputStaleError,
                GradeProposalIdempotencyConflictError,
            ),
        ):
            raise
        if isinstance(error, DBAPIError):
            raise GradeProposalRegistrationRolledBackError from error
        if isinstance(error, GradeProposalRegistrationRolledBackError):
            raise
        raise GradeProposalRegistrationRolledBackError from error


def _check_same_request(
    record: GradeProposalArtifact,
    proposal: GradeProposalDocument,
) -> None:
    if (
        record.request_fingerprint != proposal.request_fingerprint
        or record.grade_input_artifact_id != proposal.grade_input_artifact_id
        or record.grade_input_sha256 != proposal.grade_input_sha256
    ):
        raise GradeProposalIdempotencyConflictError("request_id_payload_conflict")


async def _rollback_before_commit(transaction) -> None:
    if not transaction.is_active:
        return
    try:
        await transaction.rollback()
    except Exception as error:
        logger.warning("GradeProposal pre-commit rollback failed (%s)", type(error).__name__)
