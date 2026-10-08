"""Student submission API tests backed by PostgreSQL and a real S3-compatible server."""

import asyncio
import hashlib
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from huipi_cloud.core.config import Settings, settings
from huipi_cloud.infrastructure.storage.dependencies import get_object_storage
from huipi_cloud.infrastructure.storage.s3 import S3ObjectStorage
from huipi_cloud.main import app

PDF_DATA = b"%PDF-1.7\nminimal test PDF payload\n%%EOF"
PNG_DATA = b"\x89PNG\r\n\x1a\nsmall test image"
JPEG_DATA = b"\xff\xd8\xff\xe0minimal JPEG test data\xff\xd9"


@pytest.mark.anyio
async def test_published_assignment_accepts_pdf_and_records_pending_task(
    submission_client: httpx.AsyncClient,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    response = await _upload(
        submission_client,
        assignment_id,
        filename="../folder/学生作业.pdf",
        content=PDF_DATA,
        content_type="application/pdf",
    )

    assert response.status_code == 201
    submission = response.json()
    assert submission["status"] == "submitted"
    assert submission["student_ref"] == "student-001"
    assert submission["file"]["original_filename"] == "学生作业.pdf"
    assert submission["file"]["size_bytes"] == len(PDF_DATA)
    assert submission["file"]["sha256"] == hashlib.sha256(PDF_DATA).hexdigest()
    assert submission["parsing_task"]["status"] == "pending"


@pytest.mark.anyio
async def test_png_and_jpeg_uploads_are_supported(
    submission_client: httpx.AsyncClient,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    png = await _upload(
        submission_client,
        assignment_id,
        filename="answer.png",
        content=PNG_DATA,
        content_type="image/png",
    )
    jpeg = await _upload(
        submission_client,
        assignment_id,
        filename="answer.jpg",
        content=JPEG_DATA,
        content_type="image/jpeg",
        student_ref="student-002",
    )

    assert png.status_code == jpeg.status_code == 201
    assert png.json()["file"]["content_type"] == "image/png"
    assert jpeg.json()["file"]["content_type"] == "image/jpeg"


@pytest.mark.anyio
async def test_unpublished_and_missing_assignments_are_rejected(
    submission_client: httpx.AsyncClient,
) -> None:
    draft = await _create_assignment(submission_client)
    unpublished = await _upload(
        submission_client,
        draft,
        filename="answer.pdf",
        content=PDF_DATA,
        content_type="application/pdf",
    )
    missing = await _upload(
        submission_client,
        "00000000-0000-0000-0000-000000000001",
        filename="answer.pdf",
        content=PDF_DATA,
        content_type="application/pdf",
    )

    assert unpublished.status_code == 409
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_unsupported_extension_and_mismatched_signature_are_rejected(
    submission_client: httpx.AsyncClient,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    unsupported = await _upload(
        submission_client,
        assignment_id,
        filename="answer.txt",
        content=b"not a supported file",
        content_type="text/plain",
    )
    mismatched = await _upload(
        submission_client,
        assignment_id,
        filename="answer.pdf",
        content=PNG_DATA,
        content_type="application/pdf",
    )

    assert unsupported.status_code == 415
    assert mismatched.status_code == 415


@pytest.mark.anyio
async def test_upload_enforces_configured_size_limit(
    submission_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    monkeypatch.setattr(settings, "max_upload_size_bytes", 16)
    response = await _upload(
        submission_client,
        assignment_id,
        filename="too-large.pdf",
        content=PDF_DATA,
        content_type="application/pdf",
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "文件超过大小限制"}


@pytest.mark.anyio
async def test_upload_accepts_file_exactly_at_configured_size_limit(
    submission_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    monkeypatch.setattr(settings, "max_upload_size_bytes", len(PDF_DATA))
    response = await _upload(
        submission_client,
        assignment_id,
        filename="at-limit.pdf",
        content=PDF_DATA,
        content_type="application/pdf",
    )

    assert response.status_code == 201
    assert response.json()["file"]["size_bytes"] == len(PDF_DATA)


@pytest.mark.anyio
async def test_invalid_student_ref_is_rejected(
    submission_client: httpx.AsyncClient,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    response = await _upload(
        submission_client,
        assignment_id,
        filename="answer.pdf",
        content=PDF_DATA,
        content_type="application/pdf",
        student_ref="student with spaces",
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_duplicate_student_submission_returns_conflict(
    submission_client: httpx.AsyncClient,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    first = await _upload(
        submission_client,
        assignment_id,
        filename="first.pdf",
        content=PDF_DATA,
        content_type="application/pdf",
    )
    second = await _upload(
        submission_client,
        assignment_id,
        filename="second.pdf",
        content=PDF_DATA + b" second",
        content_type="application/pdf",
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json() == {"detail": "该学生已提交此作业"}


@pytest.mark.anyio
async def test_submission_query_and_file_download_use_real_s3_compatible_storage(
    submission_client: httpx.AsyncClient,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    created = await _upload(
        submission_client,
        assignment_id,
        filename="答案一.pdf",
        content=PDF_DATA,
        content_type="application/pdf",
    )
    submission_id = created.json()["id"]

    detail = await submission_client.get(f"/api/v1/submissions/{submission_id}")
    listing = await submission_client.get(
        f"/api/v1/assignments/{assignment_id}/submissions"
    )
    download = await submission_client.get(f"/api/v1/submissions/{submission_id}/file")

    assert detail.status_code == 200
    assert detail.json()["id"] == submission_id
    assert detail.json()["parsing_task"]["status"] == "pending"
    assert [item["id"] for item in listing.json()] == [submission_id]
    assert download.status_code == 200
    assert download.content == PDF_DATA
    assert download.headers["content-type"] == "application/pdf"
    assert "attachment" in download.headers["content-disposition"]
    assert download.headers["cache-control"] == "private, no-store"


@pytest.mark.anyio
async def test_missing_submission_returns_404(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/submissions/00000000-0000-0000-0000-000000000001"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "提交记录不存在"}


@pytest.mark.anyio
async def test_submission_object_key_is_server_generated_and_test_prefixed(
    submission_client: httpx.AsyncClient,
    recording_minio_storage,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    created = await _upload(
        submission_client,
        assignment_id,
        filename="../../client-name.pdf",
        content=PDF_DATA,
        content_type="application/pdf",
    )
    assert created.status_code == 201
    key = next(iter(recording_minio_storage.uploaded_keys))

    assert key.startswith(f"{recording_minio_storage.object_key_prefix}/assignments/")
    assert "client-name.pdf" not in key
    assert created.json()["file"]["original_filename"] == "client-name.pdf"


@pytest.mark.anyio
async def test_minio_failure_returns_503_without_submission_record(
    submission_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
    recording_minio_storage,
) -> None:
    config = Settings()
    missing_bucket_storage = S3ObjectStorage(
        endpoint_url=config.minio_endpoint_url or "",
        access_key=config.minio_access_key or "",
        secret_key=config.minio_secret_key or "",
        bucket=f"missing-{uuid4().hex}",
        object_key_prefix=recording_minio_storage.object_key_prefix,
    )
    app.dependency_overrides[get_object_storage] = lambda: missing_bucket_storage
    assignment_id = await _create_published_assignment(submission_client)
    try:
        response = await _upload(
            submission_client,
            assignment_id,
            filename="answer.pdf",
            content=PDF_DATA,
            content_type="application/pdf",
        )
    finally:
        app.dependency_overrides[get_object_storage] = lambda: recording_minio_storage

    async with postgres_engine.connect() as connection:
        count = await connection.scalar(text("SELECT count(*) FROM submissions"))
    assert response.status_code == 503
    assert response.json() == {"detail": "对象存储暂时不可用，请稍后重试"}
    assert count == 0


@pytest.mark.anyio
async def test_database_failure_rolls_back_and_compensates_uploaded_object(
    submission_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
    recording_minio_storage,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    async with postgres_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION reject_test_parsing_task() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'private test failure marker'; END; $$"
            )
        )
        await connection.execute(
            text(
                "CREATE TRIGGER reject_test_parsing_task BEFORE INSERT "
                "ON parsing_tasks FOR EACH ROW EXECUTE FUNCTION reject_test_parsing_task()"
            )
        )

    try:
        response = await _upload(
            submission_client,
            assignment_id,
            filename="answer.pdf",
            content=PDF_DATA,
            content_type="application/pdf",
        )
    finally:
        async with postgres_engine.begin() as connection:
            await connection.execute(
                text("DROP TRIGGER IF EXISTS reject_test_parsing_task ON parsing_tasks")
            )
            await connection.execute(text("DROP FUNCTION IF EXISTS reject_test_parsing_task()"))

    assert response.status_code == 500
    assert "private test failure marker" not in response.text
    assert len(recording_minio_storage.uploaded_keys) == 1
    key = next(iter(recording_minio_storage.uploaded_keys))
    with pytest.raises(ClientError) as missing:
        recording_minio_storage.storage._client.head_object(
            Bucket=recording_minio_storage.bucket,
            Key=key,
        )
    assert missing.value.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}
    async with postgres_engine.connect() as connection:
        count = await connection.scalar(text("SELECT count(*) FROM submissions"))
    assert count == 0


@pytest.mark.anyio
async def test_concurrent_duplicate_submissions_keep_one_record_and_object(
    submission_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
    recording_minio_storage,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    request_gate = asyncio.Barrier(3)
    recording_minio_storage.after_upload_barrier = asyncio.Barrier(2)

    async def upload_once(content: bytes) -> httpx.Response:
        await request_gate.wait()
        return await _upload(
            submission_client,
            assignment_id,
            filename="answer.pdf",
            content=content,
            content_type="application/pdf",
        )

    requests = [
        asyncio.create_task(upload_once(PDF_DATA + b" one")),
        asyncio.create_task(upload_once(PDF_DATA + b" two")),
    ]
    await request_gate.wait()
    responses = await asyncio.gather(*requests)

    assert sorted(response.status_code for response in responses) == [201, 409]
    async with postgres_engine.connect() as connection:
        submissions = await connection.scalar(text("SELECT count(*) FROM submissions"))
        parsing_tasks = await connection.scalar(text("SELECT count(*) FROM parsing_tasks"))
    assert submissions == parsing_tasks == 1

    existing_objects = 0
    for key in recording_minio_storage.uploaded_keys:
        try:
            recording_minio_storage.storage._client.head_object(
                Bucket=recording_minio_storage.bucket,
                Key=key,
            )
        except ClientError as error:
            assert error.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}
        else:
            existing_objects += 1
    assert existing_objects == 1


@pytest.mark.anyio
async def test_uncertain_commit_keeps_object_for_possible_committed_submission(
    submission_client: httpx.AsyncClient,
    postgres_engine: AsyncEngine,
    recording_minio_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment_id = await _create_published_assignment(submission_client)
    original_commit = AsyncSession.commit

    async def commit_then_lose_acknowledgement(session: AsyncSession) -> None:
        await original_commit(session)
        raise OperationalError("COMMIT", {}, RuntimeError("simulated lost acknowledgement"))

    monkeypatch.setattr(AsyncSession, "commit", commit_then_lose_acknowledgement)
    response = await _upload(
        submission_client,
        assignment_id,
        filename="answer.pdf",
        content=PDF_DATA,
        content_type="application/pdf",
    )

    assert response.status_code == 503
    assert "simulated lost acknowledgement" not in response.text
    assert len(recording_minio_storage.uploaded_keys) == 1
    object_key = next(iter(recording_minio_storage.uploaded_keys))
    stored_object = recording_minio_storage.storage._client.head_object(
        Bucket=recording_minio_storage.bucket,
        Key=object_key,
    )
    assert stored_object["ContentLength"] == len(PDF_DATA)
    async with postgres_engine.connect() as connection:
        submissions = await connection.scalar(text("SELECT count(*) FROM submissions"))
        parsing_tasks = await connection.scalar(text("SELECT count(*) FROM parsing_tasks"))
    assert submissions == parsing_tasks == 1


@pytest.mark.anyio
async def test_submission_openapi_documents_multipart_upload() -> None:
    operation = app.openapi()["paths"][
        "/api/v1/assignments/{assignment_id}/submissions"
    ]["post"]
    form_schema_ref = operation["requestBody"]["content"]["multipart/form-data"]["schema"][
        "$ref"
    ]
    form_schema = app.openapi()["components"]["schemas"][form_schema_ref.rsplit("/", 1)[-1]]

    assert "201" in operation["responses"]
    assert {"400", "404", "409", "413", "415", "422", "500", "503"}.issubset(
        operation["responses"]
    )
    assert form_schema["required"] == ["student_ref", "file"]
    assert set(form_schema["properties"]) == {"student_ref", "file"}


async def _create_assignment(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/v1/assignments",
        json={"title": "作业提交测试", "subject": "数学", "grade_level": "高一"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _create_published_assignment(client: httpx.AsyncClient) -> str:
    assignment_id = await _create_assignment(client)
    question = await client.post(
        f"/api/v1/assignments/{assignment_id}/questions",
        json={
            "question_number": 1,
            "question_type": "short_answer",
            "stem": "说明计算过程。",
            "max_score": "10.00",
        },
    )
    assert question.status_code == 201
    question_id = question.json()["id"]
    answer = await client.put(
        f"/api/v1/questions/{question_id}/answer-key",
        json={"answer_content": "参考答案"},
    )
    rubric = await client.put(
        f"/api/v1/questions/{question_id}/rubric",
        json={"criteria": [{"description": "答案正确", "points": "10.00", "sort_order": 1}]},
    )
    assert answer.status_code == rubric.status_code == 200
    published = await client.post(f"/api/v1/assignments/{assignment_id}/publish")
    assert published.status_code == 200
    return assignment_id


async def _upload(
    client: httpx.AsyncClient,
    assignment_id: str,
    *,
    filename: str,
    content: bytes,
    content_type: str,
    student_ref: str = "student-001",
) -> httpx.Response:
    return await client.post(
        f"/api/v1/assignments/{assignment_id}/submissions",
        data={"student_ref": student_ref},
        files={"file": (filename, content, content_type)},
    )
