"""Canonical normalization persistence with the real test PostgreSQL and MinIO."""

import asyncio
import hashlib
import json
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, func, inspect, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError
from huipi_cloud.modules.canonical_documents import service
from huipi_cloud.modules.canonical_documents.errors import (
    CanonicalNormalizationError,
    CanonicalSourceNotReadyError,
)
from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact
from huipi_cloud.modules.parsing.models import ParsedArtifact
from huipi_cloud.modules.submissions.models import ParsingTask

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "mineru"


@pytest.mark.anyio
async def test_successful_normalization_is_idempotent_and_serves_verified_pages(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    submission_id, parsed_artifact_id = await _seed_parsed_source(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    before = await submission_client.get(
        f"/api/v1/submissions/{submission_id}/canonical-document"
    )
    assert before.status_code == 200
    assert before.json()["status"] == "not_generated"
    not_ready = await submission_client.get(
        f"/api/v1/submissions/{submission_id}/canonical-document/pages/1"
    )
    assert not_ready.status_code == 409

    first = await service.normalize_submission(
        session_factory,
        recording_minio_storage,
        submission_id,
    )
    second = await service.normalize_submission(
        session_factory,
        recording_minio_storage,
        submission_id,
    )
    assert first.status == second.status == "available"
    assert first.id == second.id
    assert first.canonical_object_key == second.canonical_object_key
    assert first.canonical_sha256 == second.canonical_sha256
    assert first.parsed_artifact_id == parsed_artifact_id

    async with session_factory() as session:
        indexed = (
            await session.scalars(
                select(CanonicalArtifact).where(
                    CanonicalArtifact.parsed_artifact_id == parsed_artifact_id
                )
            )
        ).all()
    assert len(indexed) == 1

    downloaded = await recording_minio_storage.download(first.canonical_object_key)
    payload = b"".join([chunk async for chunk in downloaded.chunks()])
    assert hashlib.sha256(payload).hexdigest() == first.canonical_sha256
    assert len(payload) == first.canonical_size_bytes
    canonical_body = json.loads(payload)
    assert canonical_body["source_artifact_id"] == str(parsed_artifact_id)
    assert "object_key" not in payload.decode("utf-8")

    summary = await submission_client.get(
        f"/api/v1/submissions/{submission_id}/canonical-document"
    )
    assert summary.status_code == 200
    response = summary.json()
    assert response["status"] == "available"
    assert response["source_parsing_status"] == "succeeded"
    assert response["canonical_document"]["document_id"] == str(first.id)
    assert "canonical_object_key" not in summary.text
    assert first.canonical_object_key not in summary.text

    page = await submission_client.get(
        f"/api/v1/submissions/{submission_id}/canonical-document/pages/1"
    )
    assert page.status_code == 200, page.text
    assert page.json()["page"]["blocks"][0]["content"]["value"] == "Canonical sample text"
    out_of_range = await submission_client.get(
        f"/api/v1/submissions/{submission_id}/canonical-document/pages/2"
    )
    assert out_of_range.status_code == 404
    missing = await submission_client.get(
        f"/api/v1/submissions/{uuid4()}/canonical-document"
    )
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_only_successful_parse_can_be_normalized(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    source = (FIXTURES / "synthetic-text.pdf").read_bytes()
    upload = await submission_client.post(
        f"/api/v1/assignments/{assignment_id}/submissions",
        data={"student_ref": "canonical-pending"},
        files={"file": ("source.pdf", source, "application/pdf")},
    )
    assert upload.status_code == 201
    submission_id = UUID(upload.json()["id"])
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    with pytest.raises(CanonicalSourceNotReadyError):
        await service.normalize_submission(
            session_factory,
            recording_minio_storage,
            submission_id,
        )

    async with session_factory() as session:
        task = await session.scalar(
            select(ParsingTask).where(ParsingTask.submission_id == submission_id)
        )
        canonical = await session.scalar(select(CanonicalArtifact))
    assert task is not None and task.status == "pending"
    assert canonical is None


@pytest.mark.anyio
async def test_source_bucket_mismatch_fails_closed_without_reading_another_bucket(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    submission_id, artifact_id = await _seed_parsed_source(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    class ReconfiguredBucket:
        bucket = f"{recording_minio_storage.bucket}-other"
        object_key_prefix = recording_minio_storage.object_key_prefix

        async def download(self, key):
            raise AssertionError("bucket mismatch must be checked before object reads")

        async def head_object(self, key):
            raise AssertionError("bucket mismatch must be checked before asset reads")

        async def upload_fileobj(self, fileobj, key, content_type):
            raise AssertionError("bucket mismatch must be checked before uploads")

    with pytest.raises(CanonicalNormalizationError, match="storage_bucket_mismatch"):
        await service.normalize_submission(
            session_factory,
            ReconfiguredBucket(),
            submission_id,
        )

    async with session_factory() as session:
        canonical = await session.scalar(
            select(CanonicalArtifact).where(
                CanonicalArtifact.parsed_artifact_id == artifact_id
            )
        )
        task = await session.scalar(
            select(ParsingTask).where(ParsingTask.submission_id == submission_id)
        )
    assert canonical is not None and canonical.status == "failed"
    assert canonical.failure_code == "storage_bucket_mismatch"
    assert canonical.bucket == recording_minio_storage.bucket
    assert task is not None and task.status == "succeeded"


@pytest.mark.anyio
async def test_image_manifest_reference_is_verified_and_private_key_is_not_in_canonical_json(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    submission_id, artifact_id = await _seed_parsed_source(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        with_image_asset=True,
    )
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    canonical = await service.normalize_submission(
        session_factory,
        recording_minio_storage,
        submission_id,
    )
    downloaded = await recording_minio_storage.download(canonical.canonical_object_key)
    payload = b"".join([chunk async for chunk in downloaded.chunks()])
    assert b"private-canonical-image-object-key" not in payload
    result = json.loads(payload)
    assert result["assets"][0]["source_path"] == "figures/diagram.png"
    reference = result["pages"][0]["blocks"][0]["asset_refs"][0]
    assert reference["kind"] == "stored"
    assert reference["asset_id"] == result["assets"][0]["asset_id"]
    page = await submission_client.get(
        f"/api/v1/submissions/{submission_id}/canonical-document/pages/1"
    )
    assert page.status_code == 200
    assert "private-canonical-image-object-key" not in page.text
    assert "object_key" not in page.text
    async with session_factory() as session:
        indexed = await session.scalar(
            select(CanonicalArtifact).where(
                CanonicalArtifact.parsed_artifact_id == artifact_id
            )
        )
    assert indexed is not None and indexed.status == "available"


@pytest.mark.anyio
async def test_middle_json_checksum_failure_is_recorded_without_changing_parser_success(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    submission_id, artifact_id = await _seed_parsed_source(
        submission_client,
        recording_minio_storage,
        postgres_engine,
        wrong_middle_checksum=True,
    )
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    with pytest.raises(CanonicalNormalizationError, match="source_checksum_mismatch"):
        await service.normalize_submission(
            session_factory,
            recording_minio_storage,
            submission_id,
        )

    async with session_factory() as session:
        task = await session.scalar(
            select(ParsingTask).where(ParsingTask.submission_id == submission_id)
        )
        failed = await session.scalar(
            select(CanonicalArtifact).where(
                CanonicalArtifact.parsed_artifact_id == artifact_id
            )
        )
    assert task is not None and task.status == "succeeded"
    assert failed is not None and failed.status == "failed"
    assert failed.failure_code == "source_checksum_mismatch"
    status_response = await submission_client.get(
        f"/api/v1/submissions/{submission_id}/canonical-document"
    )
    assert status_response.json()["status"] == "failed"
    assert status_response.json()["failure_code"] == "source_checksum_mismatch"
    failed_page = await submission_client.get(
        f"/api/v1/submissions/{submission_id}/canonical-document/pages/1"
    )
    assert failed_page.status_code == 409
    assert "database" not in failed_page.text.casefold()


@pytest.mark.anyio
async def test_object_storage_failure_does_not_create_success_index(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    submission_id, artifact_id = await _seed_parsed_source(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)

    class FailingCanonicalUpload:
        bucket = recording_minio_storage.bucket
        object_key_prefix = recording_minio_storage.object_key_prefix

        async def download(self, key):
            return await recording_minio_storage.download(key)

        async def head_object(self, key):
            return await recording_minio_storage.head_object(key)

        async def upload_fileobj(self, fileobj, key, content_type):
            if "/canonical/" in f"/{key}/":
                raise StorageUnavailableError("injected canonical upload failure")
            await recording_minio_storage.upload_fileobj(fileobj, key, content_type)

    with pytest.raises(StorageUnavailableError):
        await service.normalize_submission(
            session_factory,
            FailingCanonicalUpload(),
            submission_id,
        )

    async with session_factory() as session:
        canonical = await session.scalar(
            select(CanonicalArtifact).where(
                CanonicalArtifact.parsed_artifact_id == artifact_id
            )
        )
        task = await session.scalar(
            select(ParsingTask).where(ParsingTask.submission_id == submission_id)
        )
    assert canonical is not None and canonical.status == "failed"
    assert canonical.failure_code == "object_store_unavailable"
    assert task is not None and task.status == "succeeded"


@pytest.mark.anyio
async def test_database_failure_after_object_upload_leaves_no_index_and_keeps_immutable_object(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    submission_id, artifact_id = await _seed_parsed_source(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    before_keys = set(recording_minio_storage.uploaded_keys)

    def fail_canonical_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO CANONICAL_ARTIFACTS"):
            raise OperationalError(statement, parameters, OSError("injected index failure"))

    event.listen(postgres_engine.sync_engine, "before_cursor_execute", fail_canonical_insert)
    try:
        with pytest.raises(OperationalError):
            await service.normalize_submission(
                session_factory,
                recording_minio_storage,
                submission_id,
            )
    finally:
        event.remove(postgres_engine.sync_engine, "before_cursor_execute", fail_canonical_insert)

    new_keys = recording_minio_storage.uploaded_keys - before_keys
    canonical_keys = [key for key in new_keys if "/canonical/" in f"/{key}/"]
    assert len(canonical_keys) == 1
    head = await recording_minio_storage.head_object(canonical_keys[0])
    assert head["content_length"] > 0
    async with session_factory() as session:
        canonical = await session.scalar(
            select(CanonicalArtifact).where(
                CanonicalArtifact.parsed_artifact_id == artifact_id
            )
        )
        task = await session.scalar(
            select(ParsingTask).where(ParsingTask.submission_id == submission_id)
        )
    assert canonical is None
    assert task is not None and task.status == "succeeded"


@pytest.mark.anyio
async def test_concurrent_normalization_has_one_effective_database_index(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    submission_id, artifact_id = await _seed_parsed_source(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    first, second = await asyncio.gather(
        service.normalize_submission(session_factory, recording_minio_storage, submission_id),
        service.normalize_submission(session_factory, recording_minio_storage, submission_id),
    )

    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(CanonicalArtifact).where(
                    CanonicalArtifact.parsed_artifact_id == artifact_id
                )
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].status == "available"
    assert first.id == second.id == rows[0].id
    assert first.canonical_object_key == second.canonical_object_key == rows[0].canonical_object_key
    assert first.canonical_sha256 == second.canonical_sha256 == rows[0].canonical_sha256


@pytest.mark.anyio
async def test_corrupted_canonical_object_is_rejected_by_page_api(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
) -> None:
    submission_id, _ = await _seed_parsed_source(
        submission_client,
        recording_minio_storage,
        postgres_engine,
    )
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    canonical = await service.normalize_submission(
        session_factory,
        recording_minio_storage,
        submission_id,
    )
    await recording_minio_storage.upload_fileobj(
        BytesIO(b"tampered canonical bytes"),
        canonical.canonical_object_key,
        "application/json",
    )

    response = await submission_client.get(
        f"/api/v1/submissions/{submission_id}/canonical-document/pages/1"
    )

    assert response.status_code == 503
    assert "canonical_object_key" not in response.text


@pytest.mark.anyio
async def test_canonical_migration_has_unique_source_and_real_foreign_keys(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.connect() as connection:
        schema = await connection.run_sync(_canonical_table_schema)

    assert "uq_canonical_artifacts_source_normalizer" in schema["unique_constraints"]
    assert "parsed_artifacts" in schema["foreign_tables"]
    assert "submissions" in schema["foreign_tables"]
    assert "ix_canonical_artifacts_submission_created_at" in schema["indexes"]
    assert "ck_canonical_artifacts_result_shape" in schema["check_constraints"]


async def _seed_parsed_source(
    client: httpx.AsyncClient,
    storage,
    engine: AsyncEngine,
    *,
    wrong_middle_checksum: bool = False,
    with_image_asset: bool = False,
) -> tuple[UUID, UUID]:
    assignment_id = await _create_published_assignment(client)
    source = (FIXTURES / "synthetic-text.pdf").read_bytes()
    upload = await client.post(
        f"/api/v1/assignments/{assignment_id}/submissions",
        data={"student_ref": f"canonical-{uuid4().hex[:12]}"},
        files={"file": ("source.pdf", source, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    submission_id = UUID(upload.json()["id"])
    image_bytes = b"synthetic png asset" if with_image_asset else None
    image_key = (
        f"{storage.object_key_prefix}/private-canonical-image-object-key"
        if with_image_asset
        else None
    )
    image_digest = hashlib.sha256(image_bytes).hexdigest() if image_bytes is not None else None
    block = (
        {
            "type": "image",
            "index": 0,
            "content": [
                {
                    "type": "image_body",
                    "index": 0,
                    "content": '<img src="figures/diagram.png">',
                }
            ],
        }
        if with_image_asset
        else {
            "type": "text",
            "index": 0,
            "bbox": [0.1, 0.1, 0.9, 0.25],
            "content": "Canonical sample text",
        }
    )
    middle = {
        "schema": "docvortex.middle",
        "schema_version": "2.0",
        "metadata": {
            "file_suffix": "pdf",
            "producer": {"name": "mineru", "version": "4.0.10"},
            "document": {},
        },
        "extensions": {"mineru": {"tier": "basic", "parse_mode": "txt"}},
        "pages": [
            {
                "page_idx": 0,
                "blocks": [block],
            }
        ],
        "is_full_document": True,
    }
    middle_bytes = json.dumps(middle, ensure_ascii=False, separators=(",", ":")).encode()
    manifest_records = (
        [
            {
                "path": "figures/diagram.png",
                "object_key": image_key,
                "sha256": image_digest,
                "size_bytes": len(image_bytes),
                "content_type": "image/png",
            }
        ]
        if image_bytes is not None
        else []
    )
    manifest_bytes = json.dumps(
        {"schema": "huipi.parsed-assets", "version": "1", "assets": manifest_records},
        separators=(",", ":"),
    ).encode()
    middle_key = f"{storage.object_key_prefix}/p2c-source/{submission_id}/middle.json"
    manifest_key = f"{storage.object_key_prefix}/p2c-source/{submission_id}/manifest.json"
    await storage.upload_fileobj(BytesIO(middle_bytes), middle_key, "application/json")
    await storage.upload_fileobj(BytesIO(manifest_bytes), manifest_key, "application/json")
    if image_bytes is not None and image_key is not None:
        await storage.upload_fileobj(BytesIO(image_bytes), image_key, "image/png")
    task_id = UUID(upload.json()["parsing_task"]["id"])
    middle_digest = hashlib.sha256(middle_bytes).hexdigest()
    if wrong_middle_checksum:
        middle_digest = hashlib.sha256(b"wrong middle bytes").hexdigest()
    digest = hashlib.sha256(b"synthetic artifact").hexdigest()
    artifact_id = uuid4()

    async with engine.begin() as connection:
        await connection.execute(
            update(ParsingTask)
            .where(ParsingTask.id == task_id)
            .values(
                status="succeeded",
                attempt_count=1,
                started_at=func.now(),
                finished_at=func.now(),
                lease_token=None,
                lease_expires_at=None,
                next_run_at=None,
                updated_at=func.now(),
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session, session.begin():
        session.add(
            ParsedArtifact(
                id=artifact_id,
                parsing_task_id=task_id,
                submission_id=submission_id,
                bucket=storage.bucket,
                original_sha256=hashlib.sha256(source).hexdigest(),
                parser_name="MinerU",
                parser_version="4.0.10",
                tier="basic",
                schema_name="docvortex.middle",
                schema_version="2.0",
                page_count=1,
                asset_count=len(manifest_records),
                archive_key=f"{storage.object_key_prefix}/p2c-source/{submission_id}/archive.zip",
                archive_sha256=digest,
                archive_size_bytes=1,
                markdown_key=f"{storage.object_key_prefix}/p2c-source/{submission_id}/markdown.md",
                markdown_sha256=digest,
                markdown_size_bytes=1,
                middle_json_key=middle_key,
                middle_json_sha256=middle_digest,
                middle_json_size_bytes=len(middle_bytes),
                structured_content_key=f"{storage.object_key_prefix}/p2c-source/{submission_id}/structured.json",
                structured_content_sha256=digest,
                structured_content_size_bytes=1,
                assets_manifest_key=manifest_key,
                assets_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                assets_manifest_size_bytes=len(manifest_bytes),
            )
        )
    return submission_id, artifact_id


async def _create_published_assignment(client: httpx.AsyncClient) -> str:
    created = await client.post(
        "/api/v1/assignments",
        json={"title": "Canonical集成样例", "subject": "数学", "grade_level": "高一"},
    )
    assert created.status_code == 201, created.text
    assignment_id = created.json()["id"]
    question = await client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        json={
            "question_number": 1,
            "question_type": "short_answer",
            "stem": "读取合成样例。",
            "max_score": "10.00",
        },
    )
    assert question.status_code == 201, question.text
    question_id = question.json()["id"]
    answer = await client.put(
        f"/api/v1/questions/{question_id}/answer-key",
        json={"answer_content": "集成测试答案"},
    )
    rubric = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={"criteria": [{"description": "识别样例内容", "points": "10.00", "sort_order": 1}]},
    )
    assert answer.status_code == rubric.status_code == 200
    published = await client.post(f"/api/v1/assignments/{assignment_id}/publish")
    assert published.status_code == 200, published.text
    return assignment_id


def _canonical_table_schema(connection) -> dict[str, set[str]]:
    inspector = inspect(connection)
    return {
        "unique_constraints": {
            item["name"] for item in inspector.get_unique_constraints("canonical_artifacts")
        },
        "foreign_tables": {
            item["referred_table"] for item in inspector.get_foreign_keys("canonical_artifacts")
        },
        "indexes": {item["name"] for item in inspector.get_indexes("canonical_artifacts")},
        "check_constraints": {
            item["name"] for item in inspector.get_check_constraints("canonical_artifacts")
        },
    }
