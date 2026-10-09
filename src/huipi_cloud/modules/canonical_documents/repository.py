"""PostgreSQL reads and idempotent result registration for canonical documents."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huipi_cloud.modules.canonical_documents.errors import CanonicalSourceNotReadyError
from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact
from huipi_cloud.modules.canonical_documents.protocol import NORMALIZER_VERSION
from huipi_cloud.modules.parsing.enums import ParsingTaskStatus
from huipi_cloud.modules.parsing.models import ParsedArtifact
from huipi_cloud.modules.submissions.models import ParsingTask, Submission

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class NormalizationSourceRecord:
    submission: Submission
    task: ParsingTask
    parsed_artifact: ParsedArtifact


@dataclass(frozen=True)
class CanonicalStatusRecord:
    submission: Submission
    task_status: str | None
    parsed_artifact: ParsedArtifact | None
    canonical_artifact: CanonicalArtifact | None


async def get_source_for_normalization(
    session_factory: SessionFactory,
    submission_id: UUID,
) -> NormalizationSourceRecord | None:
    """Read the successful parsing tuple without holding a DB session over S3 I/O."""
    async with session_factory() as session:
        result = await session.execute(
            select(Submission, ParsingTask, ParsedArtifact)
            .outerjoin(ParsingTask, ParsingTask.submission_id == Submission.id)
            .outerjoin(ParsedArtifact, ParsedArtifact.submission_id == Submission.id)
            .where(Submission.id == submission_id)
        )
        row = result.one_or_none()
        if row is None:
            return None
        submission, task, artifact = row
        if task is None or artifact is None or task.status != ParsingTaskStatus.SUCCEEDED.value:
            raise CanonicalSourceNotReadyError("解析产物尚不可用")
        if task.id != artifact.parsing_task_id:
            raise CanonicalSourceNotReadyError("解析产物与任务记录不一致")
        return NormalizationSourceRecord(submission, task, artifact)


async def get_status_for_submission(
    session: AsyncSession,
    submission_id: UUID,
) -> CanonicalStatusRecord | None:
    """Read submission, parsing state, source artifact, and current normalizer result."""
    result = await session.execute(
        select(Submission, ParsingTask, ParsedArtifact, CanonicalArtifact)
        .outerjoin(ParsingTask, ParsingTask.submission_id == Submission.id)
        .outerjoin(
            ParsedArtifact,
            (ParsedArtifact.submission_id == Submission.id)
            & (ParsedArtifact.parsing_task_id == ParsingTask.id),
        )
        .outerjoin(
            CanonicalArtifact,
            (CanonicalArtifact.parsed_artifact_id == ParsedArtifact.id)
            & (CanonicalArtifact.normalizer_version == NORMALIZER_VERSION),
        )
        .where(Submission.id == submission_id)
    )
    row = result.one_or_none()
    if row is None:
        return None
    submission, task, parsed_artifact, canonical_artifact = row
    return CanonicalStatusRecord(
        submission=submission,
        task_status=task.status if task else None,
        parsed_artifact=parsed_artifact,
        canonical_artifact=canonical_artifact,
    )


async def get_current_canonical_artifact(
    session_factory: SessionFactory,
    parsed_artifact_id: UUID,
) -> CanonicalArtifact | None:
    async with session_factory() as session:
        result = await session.execute(
            select(CanonicalArtifact).where(
                CanonicalArtifact.parsed_artifact_id == parsed_artifact_id,
                CanonicalArtifact.normalizer_version == NORMALIZER_VERSION,
                CanonicalArtifact.status == "available",
            )
        )
        return result.scalar_one_or_none()


async def register_success(
    session_factory: SessionFactory,
    *,
    document_id: UUID,
    source: NormalizationSourceRecord,
    bucket: str,
    object_key: str,
    canonical_sha256: str,
    canonical_size_bytes: int,
    page_count: int,
    block_count: int,
) -> CanonicalArtifact:
    """Upsert success without downgrading a concurrent successful registration."""
    parsed = source.parsed_artifact
    values = {
        "id": document_id,
        "parsed_artifact_id": parsed.id,
        "submission_id": source.submission.id,
        "bucket": bucket,
        "schema_version": "1.0",
        "normalizer_version": NORMALIZER_VERSION,
        "status": "available",
        "canonical_object_key": object_key,
        "canonical_sha256": canonical_sha256,
        "canonical_size_bytes": canonical_size_bytes,
        "page_count": page_count,
        "block_count": block_count,
        "source_parser": parsed.parser_name,
        "source_parser_version": parsed.parser_version,
        "source_sha256": parsed.original_sha256,
        "failure_code": None,
    }
    update_values = {key: value for key, value in values.items() if key not in {"id"}}
    update_values["updated_at"] = func.clock_timestamp()
    statement = (
        insert(CanonicalArtifact)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[
                CanonicalArtifact.parsed_artifact_id,
                CanonicalArtifact.normalizer_version,
            ],
            set_=update_values,
            where=CanonicalArtifact.status == "failed",
        )
    )
    async with session_factory() as session, session.begin():
        await session.execute(statement)
        record = await session.scalar(
            select(CanonicalArtifact).where(
                CanonicalArtifact.parsed_artifact_id == parsed.id,
                CanonicalArtifact.normalizer_version == NORMALIZER_VERSION,
            )
        )
        if record is None:
            raise RuntimeError("canonical success registration did not produce an index")
        return record


async def register_failure(
    session_factory: SessionFactory,
    *,
    document_id: UUID,
    source: NormalizationSourceRecord,
    bucket: str,
    failure_code: str,
) -> None:
    """Record a safe deterministic failure unless another run already succeeded."""
    parsed = source.parsed_artifact
    values = {
        "id": document_id,
        "parsed_artifact_id": parsed.id,
        "submission_id": source.submission.id,
        "bucket": bucket,
        "schema_version": "1.0",
        "normalizer_version": NORMALIZER_VERSION,
        "status": "failed",
        "canonical_object_key": None,
        "canonical_sha256": None,
        "canonical_size_bytes": None,
        "page_count": None,
        "block_count": None,
        "source_parser": parsed.parser_name,
        "source_parser_version": parsed.parser_version,
        "source_sha256": parsed.original_sha256,
        "failure_code": failure_code,
    }
    update_values = {key: value for key, value in values.items() if key not in {"id"}}
    update_values["updated_at"] = func.clock_timestamp()
    statement = (
        insert(CanonicalArtifact)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[
                CanonicalArtifact.parsed_artifact_id,
                CanonicalArtifact.normalizer_version,
            ],
            set_=update_values,
            where=CanonicalArtifact.status != "available",
        )
    )
    async with session_factory() as session, session.begin():
        await session.execute(statement)
