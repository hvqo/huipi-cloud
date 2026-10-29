"""Opt-in real browser + TLS Mock OIDC + actual isolated PG/S3. No model calls."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy import select, text, update
from test_answer_review import _prepare_review
from test_answer_scripts import script_mixed_scan_pdf as mixed_scan_pdf
from test_scan_intake import (  # noqa: F401
    auth_api,
    exam,
    papers,
    roster,
    scan_context,
)

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.auth.jwt import TokenVerifier
from huipi_cloud.main import app
from huipi_cloud.modules.answer_review.models import AnswerReviewDecision
from huipi_cloud.modules.answer_scripts import service as scripts
from huipi_cloud.modules.answer_scripts.permissions import SCRIPT_OPERATOR_ROLE
from huipi_cloud.modules.exam_parsing import service as exam_parsing_service
from huipi_cloud.modules.exam_parsing.permissions import PARSE_OPERATOR_ROLE
from huipi_cloud.modules.exam_parsing.runtime import ExamBackend, ProtectedExecutor
from huipi_cloud.modules.identity import dependencies as security
from huipi_cloud.modules.identity.models import AssignmentMembership, User
from huipi_cloud.modules.resource_security import service as grants
from huipi_cloud.modules.resource_security.models import SecurityScope
from huipi_cloud.modules.scans.models import ScanFile, ScanPage
from huipi_cloud.workers.parsing import ParsingWorker


def listener():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


@pytest.mark.anyio
@pytest.mark.skipif(os.getenv("RUN_WORKBENCH_E2E") != "1", reason="real browser workbench opt-in")
async def test_teacher_workbench_browser_end_to_end(
    auth_api,  # noqa: F811
    postgres_engine,
    scan_context,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    ctx = auth_api
    if os.getenv("RUN_HOMEWORK_BROWSER") == "1":
        for classroom in scan_context.classes:
            response = await ctx.client.post(
                "/api/v1/homework/admin/teaching-assignments",
                headers=ctx.headers("admin"),
                json={
                    "teacher_id": str(ctx.users["a"].id),
                    "class_id": str(classroom.id),
                    "term_id": str(ctx.term_id),
                    "subject_id": str(ctx.subject_ids[0]),
                },
            )
            assert response.status_code == 200, response.text
        from test_exam_grading import SyntheticScoreProvider

        from huipi_cloud.modules.grading import proposal_service

        monkeypatch.setattr(
            proposal_service,
            "build_text_grading_provider",
            lambda _config: SyntheticScoreProvider(),
        )
    if os.getenv("RUN_HOMEWORK_VISUAL_BROWSER") == "1":
        from huipi_cloud.core.config import settings as app_settings
        from huipi_cloud.modules.homework import visual_service

        monkeypatch.setattr(app_settings, "grading_enabled", True)
        monkeypatch.setattr(app_settings, "grading_visual_enabled", True)
        if os.getenv("RUN_HOMEWORK_VISUAL_REAL") == "1":
            from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider

            assert app_settings.grading_provider == "openai_compatible"
            assert app_settings.grading_model == "deepseek-flash"
            assert (
                app_settings.grading_allow_remote
                and app_settings.grading_external_data_authorized
            )
            assert app_settings.grading_api_key and app_settings.grading_api_key.get_secret_value()
            assert app_settings.grading_base_url.rstrip("/") == "https://api.deepseek.com"
            real_config = app_settings.model_copy(
                update={
                    "grading_visual_enabled": True,
                    "grading_invalid_response_retries": 0,
                    "grading_deepseek_thinking": "disabled",
                    "grading_max_output_tokens": 1200,
                }
            )
            actual = build_text_grading_provider(real_config)

            class OneRealVisualRequest:
                provider_name = actual.provider_name
                model_id = actual.model_id
                generation_config_digest = actual.generation_config_digest
                calls = 0

                async def complete_visual(self, **kwargs):
                    self.calls += 1
                    if self.calls > 1:
                        raise RuntimeError("one synthetic visual request only")
                    assert len(kwargs["images"]) == 1
                    assert kwargs["images"][0].startswith(b"\x89PNG")
                    text_bytes = len(
                        (kwargs["system_prompt"] + kwargs["user_prompt"]).encode("utf-8")
                    )
                    # Conservative peak-hour bound: $0.30/M input, $1.20/M output.
                    maximum_usd = (
                        (text_bytes + 256 + 1024) * 0.30 + 1200 * 1.20
                    ) / 1_000_000
                    assert maximum_usd <= 0.05
                    response = await actual.complete_visual(**kwargs)
                    output = Path(os.environ["HOMEWORK_VISUAL_ACCEPTANCE_DIR"])
                    output.mkdir(parents=True, exist_ok=True)
                    payload = {
                        "model_id": response.model_id,
                        "elapsed_ms": response.elapsed_ms,
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "cache_hit_tokens": response.usage.cache_hit_tokens,
                        "cache_miss_tokens": response.usage.cache_miss_tokens,
                        "image_count": len(kwargs["images"]),
                        "finish_reason": response.finish_reason,
                    }
                    (output / "model-usage.json").write_text(json.dumps(payload))
                    (output / "synthetic-model-response.json").write_text(response.content)
                    (output / "model-usage.json").chmod(0o600)
                    (output / "synthetic-model-response.json").chmod(0o600)
                    return response

            visual_provider = OneRealVisualRequest()
        else:
            from test_homework_visual import ImageScoreProvider

            visual_provider = ImageScoreProvider()
        monkeypatch.setattr(
            visual_service, "build_text_grading_provider", lambda _config: visual_provider
        )
    async with ctx.factory() as db:
        await scripts.enable_operator(
            db,
            ctx.users["admin"].id,
            ctx.users["admin"].id,
            uuid4(),
            "synthetic browser grouping deployment",
        )
    async with ctx.factory() as db:
        scope_id = await db.scalar(
            select(SecurityScope.id).where(
                SecurityScope.exam_session_id == UUID(scan_context.session["id"])
            )
        )
    async with ctx.factory() as db:
        await grants.grant_role(
            db,
            ctx.principals["admin"],
            uuid4(),
            user_id=ctx.users["a"].id,
            role_version_id=SCRIPT_OPERATOR_ROLE,
            scope_id=scope_id,
        )
    async with ctx.factory() as db:
        await exam_parsing_service.enable_operator(
            db,
            ctx.users["admin"].id,
            ctx.users["admin"].id,
            uuid4(),
            "synthetic browser protected parser deployment",
        )
    async with ctx.factory() as db:
        await grants.grant_role(
            db,
            ctx.principals["admin"],
            uuid4(),
            user_id=ctx.users["a"].id,
            role_version_id=PARSE_OPERATOR_ROLE,
            scope_id=scope_id,
        )
    if os.getenv("RUN_EXAM_GRADING_BROWSER") == "1" or os.getenv("RUN_EXAM_VISUAL_BROWSER") == "1":
        from huipi_cloud.modules.exam_grading import service as marking
        from huipi_cloud.modules.exam_grading.permissions import MARK_OPERATOR_ROLE

        async with ctx.factory() as db:
            await marking.enable_operator(
                db,
                ctx.users["admin"].id,
                ctx.users["admin"].id,
                uuid4(),
                "synthetic exam marking browser deployment",
            )
        async with ctx.factory() as db:
            mark_grant = await grants.grant_role(
                db,
                ctx.principals["admin"],
                uuid4(),
                user_id=ctx.users["a"].id,
                role_version_id=UUID(MARK_OPERATOR_ROLE),
                scope_id=scope_id,
            )
        if (
            os.getenv("RUN_EXAM_RESULTS_BROWSER") == "1"
            or os.getenv("RUN_EXAM_VISUAL_BROWSER") == "1"
        ):
            from huipi_cloud.modules.exam_results.access import grant_class

            for cls in scan_context.classes:
                async with ctx.factory() as db:
                    await grant_class(
                        db,
                        ctx.principals["admin"],
                        uuid4(),
                        grant_id=UUID(mark_grant["grant_id"]),
                        session_id=UUID(scan_context.session["id"]),
                        class_id=cls.id,
                        export_allowed=True,
                    )
        if os.getenv("RUN_EXAM_FINAL_BROWSER") == "1":
            from huipi_cloud.modules.exam_results import final_service
            from huipi_cloud.modules.exam_results.access import grant_class
            from huipi_cloud.modules.exam_results.permissions import RESULT_MANAGER_ROLE

            async with ctx.factory() as db:
                await final_service.enable_operator(
                    db,
                    ctx.users["admin"].id,
                    ctx.users["admin"].id,
                    uuid4(),
                    "synthetic final results deployment",
                )
            async with ctx.factory() as db:
                final_grant = await grants.grant_role(
                    db,
                    ctx.principals["admin"],
                    uuid4(),
                    user_id=ctx.users["a"].id,
                    role_version_id=RESULT_MANAGER_ROLE,
                    scope_id=scope_id,
                )
            for cls in scan_context.classes:
                async with ctx.factory() as db:
                    await grant_class(
                        db,
                        ctx.principals["admin"],
                        uuid4(),
                        grant_id=UUID(final_grant["grant_id"]),
                        session_id=UUID(scan_context.session["id"]),
                        class_id=cls.id,
                        export_allowed=True,
                    )
    if os.getenv("RUN_PAPER_WORKBENCH_BROWSER") == "1":
        from huipi_cloud.modules.papers.permissions import PAPER_OPERATOR_ROLE

        async with ctx.factory() as db:
            await grants.grant_role(
                db,
                ctx.principals["admin"],
                uuid4(),
                user_id=ctx.users["a"].id,
                role_version_id=PAPER_OPERATOR_ROLE,
                scope_id=grants.INSTALLATION_SCOPE,
            )
    if os.getenv("RUN_OMR_BROWSER") == "1":
        from huipi_cloud.modules.exam_grading import service as marking
        from huipi_cloud.modules.exam_grading.permissions import MARK_OPERATOR_ROLE
        from huipi_cloud.modules.exam_results.access import grant_class
        from huipi_cloud.modules.papers.permissions import PAPER_OPERATOR_ROLE

        async with ctx.factory() as db:
            await marking.enable_operator(
                db, ctx.users["admin"].id, ctx.users["admin"].id, uuid4(), "synthetic OMR browser"
            )
        for role, scope in [
            (PAPER_OPERATOR_ROLE, grants.INSTALLATION_SCOPE),
            (UUID(MARK_OPERATOR_ROLE), scope_id),
        ]:
            async with ctx.factory() as db:
                value = await grants.grant_role(
                    db,
                    ctx.principals["admin"],
                    uuid4(),
                    user_id=ctx.users["a"].id,
                    role_version_id=role,
                    scope_id=scope,
                )
                if role == UUID(MARK_OPERATOR_ROLE):
                    omr_grant = value
        for cls in scan_context.classes:
            async with ctx.factory() as db:
                await grant_class(
                    db,
                    ctx.principals["admin"],
                    uuid4(),
                    grant_id=UUID(omr_grant["grant_id"]),
                    session_id=UUID(scan_context.session["id"]),
                    class_id=cls.id,
                    export_allowed=True,
                )
    creator_source = None
    if os.getenv("RUN_PAPER_CREATOR_BROWSER") == "1":
        from huipi_cloud.modules.papers.permissions import PAPER_CREATOR_ROLE

        # Separate user: never receives the test operator roles granted to actor a above.
        async with ctx.factory() as db, db.begin():
            teacher = User(
                issuer=ctx.users["a"].issuer, subject=str(uuid4()), is_active=True, is_admin=False
            )
            db.add(teacher)
            await db.flush()
            ctx.users["creator"] = teacher
        async with ctx.factory() as db:
            await grants.grant_role(
                db,
                ctx.principals["admin"],
                uuid4(),
                user_id=teacher.id,
                role_version_id=PAPER_CREATOR_ROLE,
                scope_id=grants.INSTALLATION_SCOPE,
            )
        response = await ctx.client.post(
            "/api/v1/assignments",
            headers=ctx.headers("creator"),
            json=dict(title="普通教师自己的命题作业", subject="数学", grade_level="高二"),
        )
        assert response.status_code == 201, response.text
        creator_source = response.json()["id"]
        for number in (1, 2):
            response = await ctx.client.post(
                f"/api/v1/assignments/{creator_source}/questions",
                headers=ctx.headers("creator"),
                json=dict(
                    question_number=number,
                    question_type="short_answer",
                    stem=f"合成题{number}：3+3",
                    max_score="10.00",
                ),
            )
            assert response.status_code == 201, response.text
            qid = response.json()["id"]
            assert (
                await ctx.client.put(
                    f"/api/v1/questions/{qid}/answer-key",
                    headers=ctx.headers("creator"),
                    json=dict(answer_content="6"),
                )
            ).status_code == 200
            assert (
                await ctx.client.put(
                    f"/api/v1/questions/{qid}/rubric",
                    headers=ctx.headers("creator"),
                    json=dict(
                        criteria=[
                            dict(description="方法正确", points="5.00", sort_order=1),
                            dict(description="结果正确", points="5.00", sort_order=2),
                        ]
                    ),
                )
            ).status_code == 200
        assert (
            await ctx.client.post(
                f"/api/v1/assignments/{creator_source}/publish", headers=ctx.headers("creator")
            )
        ).status_code == 200
    config_paper_b = None
    config_year_id = None
    if os.getenv("RUN_EXAM_CONFIGURATION_BROWSER") == "1":
        from test_paper_versions import create as create_paper
        from test_paper_versions import lock as lock_paper

        from huipi_cloud.modules.exam_rosters.permissions import ROSTER_OPERATOR_ROLE
        from huipi_cloud.modules.exams.permissions import EXAM_OPERATOR_ROLE
        from huipi_cloud.modules.organization.models import AcademicTerm
        from huipi_cloud.modules.scans.permissions import SCAN_OPERATOR_ROLE

        config_paper_b = await lock_paper(ctx, await create_paper(ctx, title="网页合成B卷"))
        async with ctx.factory() as db:
            config_year_id = await db.scalar(
                select(AcademicTerm.academic_year_id).where(AcademicTerm.id == ctx.term_id)
            )
        # Explicit TEST provisioning. Production configuration never grants these roles.
        for role in [
            EXAM_OPERATOR_ROLE,
            ROSTER_OPERATOR_ROLE,
            grants.ORGANIZATION_OPERATOR_ROLE,
            SCAN_OPERATOR_ROLE,
            SCRIPT_OPERATOR_ROLE,
        ]:
            async with ctx.factory() as db:
                await grants.grant_role(
                    db,
                    ctx.principals["admin"],
                    uuid4(),
                    user_id=ctx.users["a"].id,
                    role_version_id=role,
                    scope_id=grants.INSTALLATION_SCOPE,
                )
    if os.getenv("RUN_SCHOOL_WORKBENCH_BROWSER") == "1":
        from test_school_workbench import preview_role

        from huipi_cloud.modules.exam_rosters.permissions import ROSTER_OPERATOR_ROLE
        from huipi_cloud.modules.exams.permissions import EXAM_OPERATOR_ROLE
        from huipi_cloud.modules.papers.permissions import PAPER_CREATOR_ROLE

        # Explicit synthetic deployment approvals, separate from all existing test actors.
        for name in ["school_admin", "school_teacher", "school_previewer"]:
            async with ctx.factory() as db, db.begin():
                user = User(
                    issuer=ctx.users["a"].issuer,
                    subject=str(uuid4()),
                    is_active=True,
                    is_admin=False,
                )
                db.add(user)
                await db.flush()
                ctx.users[name] = user
        preview_id = await preview_role(ctx)
        for name, roles in {
            "school_admin": [
                grants.ORGANIZATION_MANAGER_ROLE,
                grants.STUDENT_IMPORTER_ROLE,
                EXAM_OPERATOR_ROLE,
                ROSTER_OPERATOR_ROLE,
            ],
            "school_teacher": [PAPER_CREATOR_ROLE],
            "school_previewer": [preview_id],
        }.items():
            for role in roles:
                async with ctx.factory() as db:
                    await grants.grant_role(
                        db,
                        ctx.principals["admin"],
                        uuid4(),
                        user_id=ctx.users[name].id,
                        role_version_id=role,
                        scope_id=grants.INSTALLATION_SCOPE,
                    )
    marking_teacher_ids = {}
    if os.getenv("RUN_MARKING_TASKS_BROWSER") == "1" or os.getenv("RUN_RECOVERY_BROWSER") == "1":
        from huipi_cloud.modules.marking_tasks import service as tasks_service
        from huipi_cloud.modules.marking_tasks.permissions import MANAGER_ROLE, TEACHER_ROLE

        async with ctx.factory() as db:
            await tasks_service.enable_operator(
                db,
                ctx.users["admin"].id,
                ctx.users["admin"].id,
                uuid4(),
                "synthetic marking task approval",
            )
        for name in ["marker_a", "marker_b"]:
            async with ctx.factory() as db, db.begin():
                u = User(
                    issuer=ctx.users["a"].issuer,
                    subject=str(uuid4()),
                    is_active=True,
                    is_admin=False,
                )
                db.add(u)
                await db.flush()
                ctx.users[name] = u
                marking_teacher_ids[name] = str(u.id)
            async with ctx.factory() as db:
                await grants.grant_role(
                    db,
                    ctx.principals["admin"],
                    uuid4(),
                    user_id=u.id,
                    role_version_id=TEACHER_ROLE,
                    scope_id=scope_id,
                )
        async with ctx.factory() as db:
            await grants.grant_role(
                db,
                ctx.principals["admin"],
                uuid4(),
                user_id=ctx.users["a"].id,
                role_version_id=MANAGER_ROLE,
                scope_id=scope_id,
            )
    if os.getenv("RUN_RECOVERY_BROWSER") == "1":
        original_upload = ctx.storage.upload_path
        failed_uploads = set()

        async def interrupt_recovery_upload(path, key, content_type):
            if "/exam-scans/" in key and key.endswith("/original.pdf"):
                async with ctx.factory() as db:
                    file = (
                        (
                            await db.execute(
                                select(ScanFile.__table__).where(
                                    ScanFile.__table__.c.object_key == key
                                )
                            )
                        )
                        .mappings()
                        .first()
                    )
                if (
                    file
                    and file["original_filename"] == "recover-upload.pdf"
                    and file["id"] not in failed_uploads
                ):
                    failed_uploads.add(file["id"])
                    from huipi_cloud.infrastructure.storage.s3 import StorageUnavailableError

                    raise StorageUnavailableError("synthetic first upload interruption")
            return await original_upload(path, key, content_type)

        monkeypatch.setattr(ctx.storage, "upload_path", interrupt_recovery_upload)
    worker_stop = asyncio.Event()
    worker_task = None
    homework_worker_task = None
    grading_task = None
    parse_executor = None
    previous_headers = ctx.client.headers.copy()
    ctx.client.headers.update(ctx.headers())
    tasks, servers, processes, sockets = [], [], [], []
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    try:
        _, sid, question, _, _, bundle = await _prepare_review(
            ctx.client,
            ctx.storage,
            postgres_engine,
            content="第1题 印刷题干：计算2+2。\n学生作答：😀𠀀é 2+2=4。",
        )
        aid = bundle.context.assignment_id
        from io import BytesIO

        from PIL import Image

        image = BytesIO()
        Image.new("RGB", (100, 60), "white").save(image, format="PNG")
        uploaded = await ctx.client.post(
            f"/api/v1/assignments/{aid}/submissions",
            data={"student_ref": "synthetic-image"},
            files={"file": ("synthetic-image.png", image.getvalue(), "image/png")},
        )
        assert uploaded.status_code == 201
        image_sid = uploaded.json()["id"]
        for name, write in [("b", True), ("reader", False)]:
            granted = await ctx.client.put(
                f"/api/v1/assignments/{aid}/members/{ctx.users[name].id}",
                headers=ctx.headers("admin"),
                json={"can_write": write},
            )
            assert granted.status_code == 200
        api_socket, oidc_socket, frontend_socket = listener(), listener(), listener()
        sockets.extend([api_socket, oidc_socket])
        api_port, oidc_port, frontend_port = [
            s.getsockname()[1] for s in (api_socket, oidc_socket, frontend_socket)
        ]
        frontend_socket.close()
        origin = f"http://127.0.0.1:{frontend_port}"
        issuer = f"https://127.0.0.1:{oidc_port}"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = {
            **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
            "kid": "browser-runtime",
            "alg": "RS256",
        }
        async with ctx.factory() as session, session.begin():
            await session.execute(
                update(User)
                .where(User.id.in_([u.id for u in ctx.users.values()]))
                .values(issuer=issuer)
            )
        verifier = TokenVerifier(
            Settings(
                _env_file=None,
                auth_issuer=issuer,
                auth_audience="huipi-api",
                auth_jwks_url=f"{issuer}/jwks",
            ),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"keys": [public]})
            ),
        )
        monkeypatch.setattr(security, "verifier", verifier)
        oidc = FastAPI()
        oidc.add_middleware(
            CORSMiddleware,
            allow_origins=[origin],
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "Authorization"],
        )
        codes, selection = {}, {"actor": "a", "pkce_verified": 0, "token_calls": 0}

        @oidc.get("/.well-known/openid-configuration")
        async def discovery():
            return dict(
                issuer=issuer,
                authorization_endpoint=f"{issuer}/authorize",
                token_endpoint=f"{issuer}/token",
                jwks_uri=f"{issuer}/jwks",
                end_session_endpoint=f"{issuer}/logout",
                response_types_supported=["code"],
                subject_types_supported=["public"],
                id_token_signing_alg_values_supported=["RS256"],
                token_endpoint_auth_methods_supported=["none"],
                code_challenge_methods_supported=["S256"],
            )

        @oidc.get("/jwks")
        async def jwks():
            return {"keys": [public]}

        @oidc.get("/authorize")
        async def authorize(request: Request):
            values = dict(request.query_params)
            if (
                values.get("client_id") != "teacher-workbench-test"
                or values.get("redirect_uri") != origin + "/auth/callback"
                or values.get("code_challenge_method") != "S256"
                or values.get("response_type") != "code"
            ):
                raise HTTPException(400)
            code = uuid4().hex
            codes[code] = {**values, "actor": selection["actor"]}
            return RedirectResponse(
                origin + "/auth/callback?" + urlencode({"code": code, "state": values["state"]})
            )

        @oidc.post("/token")
        async def token(request: Request):
            form = await request.form()
            saved = codes.pop(str(form.get("code")), None)
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(str(form.get("code_verifier")).encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            if (
                saved is None
                or saved["code_challenge"] != challenge
                or form.get("redirect_uri") != origin + "/auth/callback"
            ):
                raise HTTPException(400)
            selection["pkce_verified"] += 1
            selection["token_calls"] += 1
            now = int(time.time())
            claims = {
                "iss": issuer,
                "sub": ctx.users[saved["actor"]].subject,
                "iat": now - 1,
                "exp": now + 600,
            }
            access = jwt.encode(
                {**claims, "aud": "huipi-api"},
                key,
                algorithm="RS256",
                headers={"kid": "browser-runtime"},
            )
            id_token = jwt.encode(
                {
                    **claims,
                    "aud": "teacher-workbench-test",
                    "nonce": saved["nonce"],
                    "name": "合成教师",
                },
                key,
                algorithm="RS256",
                headers={"kid": "browser-runtime"},
            )
            return dict(access_token=access, id_token=id_token, token_type="Bearer", expires_in=600)

        @oidc.get("/logout")
        async def end_session(request: Request):
            return RedirectResponse(origin + "/login")

        @oidc.post("/__test__/actor/{name}")
        async def actor(name: str):
            if name not in ctx.users:
                raise HTTPException(400)
            selection["actor"] = name
            return {"ok": True}

        @oidc.post("/__test__/revoke/{name}")
        async def revoke(name: str):
            if name not in ctx.users:
                raise HTTPException(400)
            async with ctx.factory() as session, session.begin():
                await session.execute(
                    update(AssignmentMembership)
                    .where(
                        AssignmentMembership.assignment_id == aid,
                        AssignmentMembership.user_id == ctx.users[name].id,
                    )
                    .values(revoked_at=datetime.now(UTC))
                )
            return {"ok": True}

        tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(tls_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
            .sign(tls_key, hashes.SHA256())
        )
        certfile, keyfile = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
        certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        keyfile.write_bytes(
            tls_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        import ssl

        verifier.transport = httpx.AsyncHTTPTransport(
            verify=ssl.create_default_context(cafile=str(certfile)),
        )
        for target, sock, options in [
            (app, api_socket, {}),
            (oidc, oidc_socket, dict(ssl_certfile=str(certfile), ssl_keyfile=str(keyfile))),
        ]:
            server = uvicorn.Server(
                uvicorn.Config(
                    target,
                    host="127.0.0.1",
                    lifespan="off",
                    access_log=False,
                    log_level="warning",
                    **options,
                )
            )
            servers.append(server)
            tasks.append(asyncio.create_task(server.serve(sockets=[sock])))
        env = dict(
            os.environ,
            WORKBENCH_PORT=str(frontend_port),
            WORKBENCH_API_TARGET=f"http://127.0.0.1:{api_port}",
            VITE_OIDC_AUTHORITY=issuer,
            VITE_OIDC_CLIENT_ID="teacher-workbench-test",
            VITE_OIDC_SCOPE="openid profile",
        )
        vite_log = (tmp_path / "vite.log").open("wb")
        vite = await asyncio.create_subprocess_exec(
            "npm",
            "run",
            "dev",
            cwd=frontend,
            env=env,
            stdout=vite_log,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(vite)
        async with httpx.AsyncClient(trust_env=False) as client:
            async with asyncio.timeout(40):
                while True:
                    try:
                        if (await client.get(origin)).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(
                        0.1
                    )  # Bounded service readiness only; not a concurrency race.
        scan_pdf = tmp_path / "mixed-scan.pdf"
        homework_image = tmp_path / "synthetic-homework.png"
        if os.getenv("RUN_HOMEWORK_VISUAL_BROWSER") == "1":
            from test_homework_visual import synthetic_png

            homework_image.write_bytes(synthetic_png())
        if os.getenv("RUN_EXAM_VISUAL_BROWSER") == "1":
            from test_exam_visual_grading import visual_mixed_scan_pdf

            scan_pdf.write_bytes(visual_mixed_scan_pdf())
        elif os.getenv("RUN_EXAM_GRADING_BROWSER") == "1":
            from test_exam_grading import grading_mixed_scan_pdf

            scan_pdf.write_bytes(grading_mixed_scan_pdf())
        else:
            scan_pdf.write_bytes(mixed_scan_pdf())
        recovery_pdf = tmp_path / "recover-upload.pdf"
        from test_scan_intake import pdf_bytes

        recovery_pdf.write_bytes(pdf_bytes(1, label="Synthetic supplementary sheet Class 1 N001"))
        browser_env = dict(
            os.environ,
            WORKBENCH_URL=origin,
            WORKBENCH_OIDC=issuer,
            WORKBENCH_AID=str(aid),
            WORKBENCH_SID=str(sid),
            WORKBENCH_QID=str(question.id),
            WORKBENCH_IMAGE_SID=image_sid,
            WORKBENCH_SCAN_SESSION=scan_context.session["id"],
            WORKBENCH_SCAN_PDF=str(scan_pdf),
            WORKBENCH_RECOVERY_PDF=str(recovery_pdf),
            WORKBENCH_PAPER_SOURCE=str(ctx.aid),
            WORKBENCH_CREATOR_SOURCE=str(creator_source or ""),
            WORKBENCH_MARKER_A=marking_teacher_ids.get("marker_a", ""),
            WORKBENCH_MARKER_B=marking_teacher_ids.get("marker_b", ""),
            WORKBENCH_CONFIG_YEAR=str(config_year_id or ""),
            WORKBENCH_CONFIG_TERM=str(ctx.term_id),
            WORKBENCH_CONFIG_SUBJECT=str(ctx.subject_ids[0]),
            WORKBENCH_CONFIG_PAPER_A=ctx.paper["paper_version_id"],
            WORKBENCH_CONFIG_PAPER_B=config_paper_b["paper_version_id"] if config_paper_b else "",
            WORKBENCH_CONFIG_CLASS_A=str(scan_context.classes[0].id),
            WORKBENCH_CONFIG_CLASS_B=str(scan_context.classes[1].id),
            WORKBENCH_HOMEWORK_TEACHER_B=str(ctx.users["b"].id),
            WORKBENCH_HOMEWORK_IMAGE=str(homework_image),
            WORKBENCH_OMR_PAPER=ctx.paper["paper_version_id"],
            WORKBENCH_PYTHON=sys.executable,
        )
        # Dedicated exam worker consumes real PG jobs and persists real private S3 objects.
        config = Settings(
            _env_file=None,
            parsing_lease_seconds=180,
            parsing_heartbeat_seconds=2,
            parsing_poll_seconds=0.1,
            parsing_retry_base_seconds=1,
            parsing_retry_max_seconds=1,
            parsing_execution_timeout_seconds=240,
        )
        backend = ExamBackend(ctx.storage, config)
        if os.getenv("RUN_EXAM_MINERU_BROWSER") == "1":
            from huipi_cloud.infrastructure.parsing.mineru import MinerUParserExecutor

            parser = MinerUParserExecutor(
                ctx.storage,
                executable=os.environ["MINERU_EXECUTABLE"],
                mineru_home=Path(os.environ["MINERU_HOME"]),
                tier="basic",
                max_input_bytes=32 * 1024**2,
                max_pdf_pages=100,
                max_output_bytes=256 * 1024**2,
                max_text_bytes=64 * 1024**2,
                max_archive_members=10000,
                execution_timeout_seconds=240,
                cancellation_grace_seconds=3,
            )
        else:
            from test_exam_parsing import SyntheticExecutor

            parser = SyntheticExecutor(ctx.storage)
            if os.getenv("RUN_RECOVERY_BROWSER") == "1":
                normal_execute = parser.execute

                async def recoverable_failure(task):
                    async with ctx.factory() as db:
                        job = await exam_parsing_service.scripts.exams.row(
                            db, exam_parsing_service.J, task.task_id
                        )
                        version = await exam_parsing_service.scripts.exams.row(
                            db, scripts.V, job["answer_script_version_id"]
                        )
                    if version["reason"] == "异常补救：测试解析资源故障":
                        from huipi_cloud.modules.parsing.errors import RetryableParsingError

                        raise RetryableParsingError("parser_unavailable")
                    return await normal_execute(task)

                parser.execute = recoverable_failure

        parse_executor = ProtectedExecutor(parser, backend)
        worker = ParsingWorker(ctx.factory, parse_executor, config, repository_backend=backend)
        worker_task = asyncio.create_task(worker.run(worker_stop))
        if os.getenv("RUN_HOMEWORK_BROWSER") == "1":
            from test_exam_parsing import SyntheticExecutor

            legacy_parser = SyntheticExecutor(ctx.storage, text="第1题 学生作答：2+2=4。")
            legacy_worker = ParsingWorker(ctx.factory, legacy_parser, config)
            homework_worker_task = asyncio.create_task(legacy_worker.run(worker_stop))
        if os.getenv("RUN_EXAM_VISUAL_BROWSER") == "1":
            from huipi_cloud.core.config import settings as app_settings
            from huipi_cloud.modules.exam_grading.runtime import ScoringWorker

            monkeypatch.setattr(app_settings, "grading_enabled", True)
            monkeypatch.setattr(app_settings, "grading_visual_enabled", True)
            if os.getenv("RUN_VISUAL_REAL") == "1":
                from huipi_cloud.infrastructure.grading.provider import build_text_grading_provider

                assert app_settings.grading_provider == "openai_compatible"
                assert app_settings.grading_model == "deepseek-flash"
                assert (
                    app_settings.grading_allow_remote
                    and app_settings.grading_external_data_authorized
                )
                assert (
                    app_settings.grading_api_key and app_settings.grading_api_key.get_secret_value()
                )
                score_config = app_settings.model_copy(
                    update={
                        "grading_visual_enabled": True,
                        "grading_invalid_response_retries": 0,
                        "grading_deepseek_thinking": "disabled",
                        "grading_max_output_tokens": 1200,
                    }
                )
                real_provider = build_text_grading_provider(score_config)

                class RecordingVisualProvider:
                    provider_name = real_provider.provider_name
                    model_id = real_provider.model_id
                    generation_config_digest = real_provider.generation_config_digest
                    calls = 0

                    async def complete_visual(self, **kwargs):
                        self.calls += 1
                        if self.calls > 1:
                            raise RuntimeError("live visual browser request cap exceeded")
                        assert len(kwargs["images"]) == 2
                        response = await real_provider.complete_visual(**kwargs)
                        output = Path(os.environ["VISUAL_ACCEPTANCE_DIR"])
                        output.mkdir(parents=True, exist_ok=True)
                        (output / "model-response.json").write_text(
                            response.content, encoding="utf-8"
                        )
                        (output / "model-usage.json").write_text(
                            json.dumps(
                                dict(
                                    model_id=response.model_id,
                                    elapsed_ms=response.elapsed_ms,
                                    prompt_tokens=response.usage.prompt_tokens,
                                    completion_tokens=response.usage.completion_tokens,
                                    cache_hit_tokens=response.usage.cache_hit_tokens,
                                    cache_miss_tokens=response.usage.cache_miss_tokens,
                                    image_count=len(kwargs["images"]),
                                    finish_reason=response.finish_reason,
                                ),
                                ensure_ascii=False,
                            ),
                            encoding="utf-8",
                        )
                        return response

                score_provider = RecordingVisualProvider()
            else:
                from test_exam_visual_grading import SyntheticVisualProvider

                score_config = config.model_copy(
                    update={
                        "grading_enabled": True,
                        "grading_visual_enabled": True,
                    }
                )
                score_provider = SyntheticVisualProvider()
            grading_task = asyncio.create_task(
                ScoringWorker(ctx.factory, ctx.storage, score_provider, score_config).run(
                    worker_stop
                )
            )
        elif os.getenv("RUN_EXAM_GRADING_BROWSER") == "1":
            from test_exam_grading import SyntheticScoreProvider, SyntheticSelectionProvider

            from huipi_cloud.modules.exam_grading.runtime import ScoringWorker

            selection_mode = os.getenv("RUN_EXAM_SELECTION_BROWSER") == "1"
            score_config = config.model_copy(
                update={
                    "grading_strategy": "quote_selection" if selection_mode else "strict_offsets"
                }
            )
            score_provider = (
                SyntheticSelectionProvider() if selection_mode else SyntheticScoreProvider()
            )
            grading_task = asyncio.create_task(
                ScoringWorker(ctx.factory, ctx.storage, score_provider, score_config).run(
                    worker_stop
                )
            )

        browser = await asyncio.create_subprocess_exec(
            "npm",
            "run",
            "e2e",
            cwd=frontend,
            env=browser_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(browser)
        output, _ = await asyncio.wait_for(browser.communicate(), timeout=300)
        (frontend / "test-results" / "browser-summary.txt").write_text(output.decode())
        assert browser.returncode == 0, output.decode()[-12000:]
        assert selection["pkce_verified"] >= 3
        if os.getenv("RUN_MARKING_TASKS_BROWSER") == "1":
            from sqlalchemy import func

            from huipi_cloud.modules.marking_tasks.models import MarkingTask

            async with ctx.factory() as db:
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(MarkingTask)
                        .where(MarkingTask.status == "completed", MarkingTask.is_current)
                    )
                    == 3
                )
                assert (
                    await db.scalar(
                        select(func.count()).select_from(MarkingTask).where(MarkingTask.is_current)
                    )
                    == 3
                )
                teacher_ids = [ctx.users[n].id for n in ["marker_a", "marker_b"]]
                records = (
                    (await db.execute(select(AnswerReviewDecision.reviewer_user_id)))
                    .scalars()
                    .all()
                )
                assert all(user_id is not None for user_id in records)
                from huipi_cloud.modules.grading.grade_review_models import GradeReviewRevision

                assert set(
                    (
                        await db.scalars(
                            select(GradeReviewRevision.__table__.c.reviewer_user_id).where(
                                GradeReviewRevision.__table__.c.reviewer_user_id.in_(teacher_ids)
                            )
                        )
                    ).all()
                ) == set(teacher_ids)

        if os.getenv("RUN_SCHOOL_WORKBENCH_BROWSER") == "1":
            # Independently check the real database, not only the rendered list.
            async with ctx.factory() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM students WHERE school_student_code "
                            "IN ('001','002','003','004','005')"
                        )
                    )
                    == 5
                )
                assert (
                    await db.scalar(
                        text("SELECT count(*) FROM student_import_batches WHERE status='confirmed'")
                    )
                    == 2
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM roster_entry_snapshots e "
                            "JOIN roster_revisions r ON r.id=e.roster_revision_id "
                            "JOIN exam_sessions s ON s.id=r.exam_session_id "
                            "WHERE s.title='学校联动数学' AND r.status='frozen' AND r.is_current"
                        )
                    )
                    == 4
                )
                from huipi_cloud.modules.papers.permissions import PAPER_CREATOR_ROLE
                from huipi_cloud.modules.resource_security.models import SecurityGrant

                teacher_roles = (
                    await db.scalars(
                        select(SecurityGrant.role_version_id).where(
                            SecurityGrant.user_id == ctx.users["school_teacher"].id,
                            SecurityGrant.revoked_at.is_(None),
                        )
                    )
                ).all()
                assert teacher_roles == [PAPER_CREATOR_ROLE]
        if os.getenv("RUN_PAPER_CREATOR_BROWSER") == "1":
            from huipi_cloud.modules.papers.models import PaperCreatorAuthority
            from huipi_cloud.modules.papers.permissions import PAPER_AUTHOR_ROLE, PAPER_CREATOR_ROLE
            from huipi_cloud.modules.resource_security.models import SecurityGrant

            async with ctx.factory() as db:
                actor_grants = (
                    await db.execute(
                        select(
                            SecurityGrant.role_version_id,
                            SecurityScope.kind,
                            SecurityScope.paper_version_id,
                        )
                        .join(SecurityScope, SecurityGrant.scope_id == SecurityScope.id)
                        .where(SecurityGrant.user_id == ctx.users["creator"].id)
                    )
                ).all()
                assert len(actor_grants) == 3
                assert (
                    sum(r[0] == PAPER_CREATOR_ROLE and r[1] == "installation" for r in actor_grants)
                    == 1
                )
                assert (
                    sum(
                        r[0] == PAPER_AUTHOR_ROLE and r[1] == "paper_version" and r[2] is not None
                        for r in actor_grants
                    )
                    == 2
                )
                facts = (
                    await db.scalars(
                        select(PaperCreatorAuthority).where(
                            PaperCreatorAuthority.actor_user_id == ctx.users["creator"].id
                        )
                    )
                ).all()
                assert len(facts) == 2
        async with ctx.factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(AnswerReviewDecision)
                        .where(AnswerReviewDecision.submission_id == sid)
                        .order_by(AnswerReviewDecision.revision)
                    )
                ).all()
            )
        async with ctx.factory() as db:
            scan_files = list((await db.scalars(select(ScanFile))).all())
            scan_pages = list((await db.scalars(select(ScanPage))).all())
        original_sha = hashlib.sha256(scan_pdf.read_bytes()).hexdigest()
        originals = [f for f in scan_files if f.sha256 == original_sha]
        assert len(originals) == 1 and originals[0].status == "stored"
        original_pages = [p for p in scan_pages if p.file_id == originals[0].id]
        assert len(original_pages) == 4 and all(
            p.identity_status == "unconfirmed" for p in original_pages
        )
        if os.getenv("RUN_RECOVERY_BROWSER") == "1":
            assert len(scan_files) == 2 and len(scan_pages) == 5
            recovered = next(f for f in scan_files if f.original_filename == "recover-upload.pdf")
            assert recovered.status == "stored" and recovered.attempt_number == 2
            async with ctx.factory() as db:
                jobs = (await db.execute(select(exam_parsing_service.J))).mappings().all()
                assert any(j["status"] == "failed" and j["attempt_count"] == 3 for j in jobs)
                assert sum(j["status"] == "succeeded" for j in jobs) == 3
                from huipi_cloud.modules.marking_tasks import service as task_service

                assigned = (await db.execute(select(task_service.T))).mappings().all()
                assert len(assigned) == 1 and assigned[0]["status"] == "in_progress"
        elif os.getenv("RUN_OMR_BROWSER") == "1":
            assert len(scan_files) == 2 and len(scan_pages) == 5
            omr_file = next(f for f in scan_files if f.original_filename == "synthetic-filled.pdf")
            assert omr_file.status == "stored"
            assert len([p for p in scan_pages if p.file_id == omr_file.id]) == 1
            async with ctx.factory() as db:
                choices = (
                    await db.execute(
                        text("SELECT choice, points FROM omr_decisions ORDER BY choice")
                    )
                ).all()
                assert [(c, str(points)) for c, points in choices] == [
                    ("A", "10.00"),
                    ("B", "10.00"),
                ]
                assert await db.scalar(text("SELECT count(*) FROM omr_recognitions")) == 1
        else:
            assert len(scan_files) == 1 and len(scan_pages) == 4
        assert len(rows) == 3 and rows[0].decision == "response_present"
        assert all(row.reviewer_user_id is not None for row in rows)
        assert len({row.request_id for row in rows}) == len(rows)
        assert any(row.decision == "response_absent" for row in rows)
        assert any(row.decision == "uncertain" for row in rows)
        text_value = "第1题 印刷题干：计算2+2。\n学生作答：😀𠀀é 2+2=4。"
        assert rows[0].response_regions[0]["text_start"] == text_value.index("学生作答")
        assert rows[0].response_regions[0]["text_end"] == len(text_value)
    finally:
        worker_stop.set()
        if homework_worker_task is not None:
            await asyncio.wait_for(homework_worker_task, timeout=15)
        if grading_task is not None:
            await asyncio.wait_for(grading_task, timeout=15)
        if worker_task is not None:
            await asyncio.wait_for(worker_task, timeout=15)
        if parse_executor is not None:
            await parse_executor.aclose()
        async with ctx.factory() as db, db.begin():
            if os.getenv("RUN_HOMEWORK_BROWSER") == "1":
                await db.execute(
                    text(
                        "TRUNCATE homework_attempts,homework_participants,homework_classes,"
                        "homework_offerings,homework_teaching_assignments CASCADE"
                    )
                )
            if os.getenv("RUN_OMR_BROWSER") == "1":
                await db.execute(
                    text(
                        "TRUNCATE omr_operations,omr_decisions,omr_recognitions,"
                        "omr_template_questions,omr_templates CASCADE"
                    )
                )
            await db.execute(text("TRUNCATE marking_operations,marking_tasks CASCADE"))
            await db.execute(
                text(
                    "TRUNCATE exam_grading_operations,exam_scoring_jobs,exam_grading_workspaces,"
                    "grade_review_revisions,grade_proposal_artifacts,grade_input_artifacts,"
                    "answer_review_decisions,answer_alignment_artifacts,"
                    "exam_parsing_operations,exam_submission_links,"
                    "exam_parsing_jobs CASCADE"
                )
            )
            await db.execute(text("TRUNCATE answer_script_operations,answer_scripts CASCADE"))
        if os.getenv("RUN_SCHOOL_WORKBENCH_BROWSER") == "1":
            async with ctx.factory() as db, db.begin():
                await db.execute(
                    text(
                        "TRUNCATE organization_operations,student_import_batches,"
                        "academic_years,grade_cohorts,subjects,students CASCADE"
                    )
                )
        import signal

        for process in processes:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
        for server in servers:
            server.should_exit = True
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        for sock in sockets:
            sock.close()
        ctx.client.headers.clear()
        ctx.client.headers.update(previous_headers)
        async with ctx.factory() as session, session.begin():
            await session.execute(text("TRUNCATE assignments CASCADE"))
        # auth_api cleanup uses original provisioned User IDs regardless of issuer.
