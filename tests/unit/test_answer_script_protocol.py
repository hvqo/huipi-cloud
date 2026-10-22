"""No client identity, variant guesses, duplicate source or unbounded manifests."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from huipi_cloud.modules.answer_scripts.schemas import ScriptChange, ScriptCreate
from huipi_cloud.modules.answer_scripts.service import manifest_digest


def payload():
    return dict(request_id=uuid4(), page_ids=[uuid4()], reason="人工核验")


@pytest.mark.parametrize(
    "extra", ["student_id", "participation_id", "is_admin", "role", "object_key"]
)
def test_client_authority_fields_are_forbidden(extra):
    with pytest.raises(ValidationError):
        ScriptCreate(**payload(), **{extra: str(uuid4())})


@pytest.mark.parametrize("count", [0, 101])
def test_page_budget(count):
    p = payload()
    p["page_ids"] = [uuid4() for _ in range(count)]
    with pytest.raises(ValidationError):
        ScriptCreate(**p)


def test_duplicate_page_and_missing_identity_rejected():
    p = payload()
    p["page_ids"] *= 2
    with pytest.raises(ValidationError):
        ScriptCreate(**p)
    with pytest.raises(ValidationError):
        ScriptChange(**payload(), expected_revision=1, action="confirm")


def test_explicit_confirm_and_draft_identity_separation():
    p = dict(
        **payload(),
        expected_revision=1,
        roster_revision_id=uuid4(),
        roster_entry_id=uuid4(),
        session_paper_id=uuid4(),
    )
    ScriptChange(**p, action="confirm")
    with pytest.raises(ValidationError):
        ScriptChange(**p, action="save_draft")


def test_order_and_page_digest_participate_in_manifest():
    one = dict(
        id=uuid4(),
        file_id=uuid4(),
        batch_id=uuid4(),
        page_index=0,
        sha256="a" * 64,
        file_sha256="b" * 64,
    )
    two = one | dict(id=uuid4(), page_index=1)
    assert manifest_digest([one, two]) != manifest_digest([two, one])
    assert manifest_digest([one]) != manifest_digest([one | dict(sha256="c" * 64)])


@pytest.mark.anyio
async def test_grouping_body_budget_and_admission_are_bounded():
    import asyncio

    import httpx
    from fastapi import FastAPI, Request

    from huipi_cloud.api.v1.upload_limits import UploadRequestLimitMiddleware

    app = FastAPI()
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0

    @app.post("/api/v1/exam-sessions/{sid}/answer-scripts")
    async def held(request: Request):
        nonlocal active
        await request.body()
        active += 1
        if active == 2:
            entered.set()
        await release.wait()
        return {"ok": True}

    limited = UploadRequestLimitMiddleware(app, max_upload_size_bytes=100, api_v1_prefix="/api/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(limited), base_url="http://test"
    ) as client:
        path = "/api/v1/exam-sessions/s/answer-scripts"
        assert (await client.post(path, content=b"x" * 65537)).status_code == 413
        tasks = [asyncio.create_task(client.post(path, content=b"{}")) for _ in range(2)]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert (await client.post(path, content=b"{}")).status_code == 429
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert limited.active_script_operations == 0
