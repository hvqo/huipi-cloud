"""Opt-in end-to-end parsing through PostgreSQL, S3, the real worker, and MinerU."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.parsing.mineru import MinerUParserExecutor
from huipi_cloud.modules.canonical_documents import service as canonical_service
from huipi_cloud.modules.canonical_documents.models import CanonicalArtifact
from huipi_cloud.modules.parsing.enums import ParsingTaskStatus
from huipi_cloud.modules.parsing.models import ParsedArtifact
from huipi_cloud.modules.submissions.models import ParsingTask
from huipi_cloud.workers.parsing import ParsingWorker

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "mineru"
_ENABLED = os.environ.get("MINERU_E2E") == "1"


@pytest.mark.anyio
@pytest.mark.mineru_e2e
@pytest.mark.skipif(
    not _ENABLED,
    reason="set MINERU_E2E=1 and configure an isolated MinerU 4.x runtime and model cache",
)
@pytest.mark.parametrize(
    ("filename", "content_type", "expected_text"),
    [
        ("synthetic-text.pdf", "application/pdf", "HUIPI CLOUD TEXT PDF SAMPLE"),
        ("synthetic-scan.pdf", "application/pdf", "HUIPI CLOUD SCANNED SAMPLE"),
        ("synthetic-image.png", "image/png", "HUIPI CLOUD SCANNED SAMPLE"),
    ],
)
async def test_real_mineru_worker_persists_and_serves_parsed_outputs(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
    postgres_engine: AsyncEngine,
    filename: str,
    content_type: str,
    expected_text: str,
) -> None:
    """Exercise the actual parser subprocess and verify every indexed object."""
    cli_path = os.environ.get("MINERU_EXECUTABLE")
    model_home = os.environ.get("MINERU_HOME")
    assert cli_path and model_home, "MINERU_EXECUTABLE and MINERU_HOME are required"

    source = (FIXTURES / filename).read_bytes()
    assignment_id = await _create_published_assignment(submission_client)
    upload = await submission_client.post(
        f"/api/v1/assignments/{assignment_id}/submissions",
        data={"student_ref": "mineru-e2e-student"},
        files={"file": (filename, source, content_type)},
    )
    assert upload.status_code == 201, upload.text
    submission_id = UUID(upload.json()["id"])
    original_digest = hashlib.sha256(source).hexdigest()
    assert upload.json()["file"]["sha256"] == original_digest
    assert upload.json()["parsing_task"]["status"] == "pending"

    executor = MinerUParserExecutor(
        recording_minio_storage,
        executable=cli_path,
        mineru_home=Path(model_home),
        tier="basic",
        max_input_bytes=20 * 1024 * 1024,
        max_pdf_pages=200,
        max_output_bytes=256 * 1024 * 1024,
        max_text_bytes=64 * 1024 * 1024,
        max_archive_members=10_000,
        execution_timeout_seconds=240,
        cancellation_grace_seconds=3,
    )
    session_factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    worker_config = Settings(
        parsing_lease_seconds=60,
        parsing_heartbeat_seconds=15,
        parsing_poll_seconds=0.1,
        parsing_execution_timeout_seconds=240,
    )
    worker = ParsingWorker(session_factory, executor, worker_config)
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(worker.run(stop_event))
    try:
        task, artifact = await _wait_for_result(session_factory, submission_id)
        assert task.status == ParsingTaskStatus.SUCCEEDED.value
        assert artifact.submission_id == submission_id
        assert artifact.original_sha256 == original_digest
        assert artifact.parser_name == "MinerU"
        assert artifact.parser_version.startswith("4.")
        assert artifact.schema_name == "docvortex.middle"
        assert artifact.schema_version == "2.0"
        assert artifact.page_count >= 1

        summary_response = await submission_client.get(
            f"/api/v1/submissions/{submission_id}/parsed-document"
        )
        assert summary_response.status_code == 200, summary_response.text
        summary = summary_response.json()["parsed_document"]
        assert summary["artifact_id"] == str(artifact.id)
        assert summary["original_sha256"] == original_digest

        markdown_response = await submission_client.get(
            f"/api/v1/submissions/{submission_id}/parsed-document/markdown"
        )
        assert markdown_response.status_code == 200, markdown_response.text
        markdown = markdown_response.content
        assert expected_text in markdown.decode("utf-8").upper()
        assert hashlib.sha256(markdown).hexdigest() == artifact.markdown_sha256
        assert markdown_response.headers["content-type"].startswith("text/markdown")

        for key, expected_sha, expected_size in (
            (artifact.archive_key, artifact.archive_sha256, artifact.archive_size_bytes),
            (artifact.markdown_key, artifact.markdown_sha256, artifact.markdown_size_bytes),
            (
                artifact.middle_json_key,
                artifact.middle_json_sha256,
                artifact.middle_json_size_bytes,
            ),
            (
                artifact.structured_content_key,
                artifact.structured_content_sha256,
                artifact.structured_content_size_bytes,
            ),
            (
                artifact.assets_manifest_key,
                artifact.assets_manifest_sha256,
                artifact.assets_manifest_size_bytes,
            ),
        ):
            digest, size_bytes, payload = await _read_object(recording_minio_storage, key)
            assert (digest, size_bytes) == (expected_sha, expected_size)
            if key == artifact.assets_manifest_key:
                manifest = json.loads(payload)
                assert manifest["schema"] == "huipi.parsed-assets"
                assert len(manifest["assets"]) == artifact.asset_count
                for asset in manifest["assets"]:
                    asset_digest, asset_size, _ = await _read_object(
                        recording_minio_storage,
                        asset["object_key"],
                    )
                    assert (asset_digest, asset_size) == (
                        asset["sha256"],
                        asset["size_bytes"],
                    )

        canonical = await canonical_service.normalize_submission(
            session_factory,
            recording_minio_storage,
            submission_id,
        )
        assert canonical.status == "available"
        assert canonical.parsed_artifact_id == artifact.id
        assert canonical.page_count == artifact.page_count
        canonical_payload = await _read_object(
            recording_minio_storage,
            canonical.canonical_object_key,
        )
        assert canonical_payload[0] == canonical.canonical_sha256
        assert canonical_payload[1] == canonical.canonical_size_bytes

        canonical_summary = await submission_client.get(
            f"/api/v1/submissions/{submission_id}/canonical-document"
        )
        assert canonical_summary.status_code == 200, canonical_summary.text
        summary = canonical_summary.json()
        assert summary["status"] == "available"
        assert summary["canonical_document"]["source_artifact_id"] == str(artifact.id)
        assert canonical.canonical_object_key not in canonical_summary.text

        canonical_page = await submission_client.get(
            f"/api/v1/submissions/{submission_id}/canonical-document/pages/1"
        )
        assert canonical_page.status_code == 200, canonical_page.text
        page_text = " ".join(_canonical_text_values(canonical_page.json()["page"]["blocks"]))
        assert expected_text in page_text.upper()

        async with session_factory() as session:
            indexed_canonical = await session.scalar(
                select(CanonicalArtifact).where(
                    CanonicalArtifact.parsed_artifact_id == artifact.id
                )
            )
            task_after_normalization = await session.scalar(
                select(ParsingTask).where(ParsingTask.submission_id == submission_id)
            )
        assert indexed_canonical is not None and indexed_canonical.status == "available"
        assert task_after_normalization is not None
        assert task_after_normalization.status == ParsingTaskStatus.SUCCEEDED.value
    finally:
        stop_event.set()
        await asyncio.wait_for(worker_task, timeout=10)


async def _create_published_assignment(client: httpx.AsyncClient) -> str:
    created = await client.post(
        "/api/v1/assignments",
        json={"title": "MinerU合成样例", "subject": "数学", "grade_level": "高一"},
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
        json={"answer_content": "合成测试答案"},
    )
    rubric = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={"criteria": [{"description": "识别样例内容", "points": "10.00", "sort_order": 1}]},
    )
    assert answer.status_code == rubric.status_code == 200
    published = await client.post(f"/api/v1/assignments/{assignment_id}/publish")
    assert published.status_code == 200, published.text
    return assignment_id


async def _wait_for_result(session_factory, submission_id: UUID):
    deadline = asyncio.get_running_loop().time() + 300
    while asyncio.get_running_loop().time() < deadline:
        async with session_factory() as session:
            task = await session.scalar(
                select(ParsingTask).where(ParsingTask.submission_id == submission_id)
            )
            artifact = await session.scalar(
                select(ParsedArtifact).where(ParsedArtifact.submission_id == submission_id)
            )
        assert task is not None
        if task.status == ParsingTaskStatus.FAILED.value:
            pytest.fail(f"MinerU failed with safe error code: {task.last_error_code}")
        if task.status == ParsingTaskStatus.SUCCEEDED.value and artifact is not None:
            return task, artifact
        await asyncio.sleep(0.2)
    pytest.fail("MinerU task did not reach a terminal successful state within five minutes")


async def _read_object(storage, key: str) -> tuple[str, int, bytes]:
    downloaded = await storage.download(key)
    digest = hashlib.sha256()
    size = 0
    chunks: list[bytes] = []
    async for chunk in downloaded.chunks():
        digest.update(chunk)
        size += len(chunk)
        chunks.append(chunk)
    return digest.hexdigest(), size, b"".join(chunks)


def _canonical_text_values(value: object) -> list[str]:
    if isinstance(value, dict):
        result = []
        if isinstance(value.get("value"), str) and value.get("normalized_type") in {
            "text",
            "layout",
            "formula",
        }:
            result.append(value["value"])
        for key, item in value.items():
            if key not in {"value", "source_fields"}:
                result.extend(_canonical_text_values(item))
        return result
    if isinstance(value, list):
        return [text for item in value for text in _canonical_text_values(item)]
    return []
