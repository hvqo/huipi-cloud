"""Opt-in real browser + TLS Mock OIDC + actual isolated PG/S3. No model calls."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import socket
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
    if os.getenv("RUN_EXAM_GRADING_BROWSER") == "1":
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
            await grants.grant_role(
                db,
                ctx.principals["admin"],
                uuid4(),
                user_id=ctx.users["a"].id,
                role_version_id=UUID(MARK_OPERATOR_ROLE),
                scope_id=scope_id,
            )
    worker_stop = asyncio.Event()
    worker_task = None
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
        if os.getenv("RUN_EXAM_GRADING_BROWSER") == "1":
            from test_exam_grading import grading_mixed_scan_pdf

            scan_pdf.write_bytes(grading_mixed_scan_pdf())
        else:
            scan_pdf.write_bytes(mixed_scan_pdf())
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
        )
        # Dedicated exam worker consumes real PG jobs and persists real private S3 objects.
        config = Settings(
            _env_file=None,
            parsing_lease_seconds=180,
            parsing_heartbeat_seconds=2,
            parsing_poll_seconds=0.1,
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
        parse_executor = ProtectedExecutor(parser, backend)
        worker = ParsingWorker(ctx.factory, parse_executor, config, repository_backend=backend)
        worker_task = asyncio.create_task(worker.run(worker_stop))
        if os.getenv("RUN_EXAM_GRADING_BROWSER") == "1":
            from test_exam_grading import SyntheticScoreProvider, SyntheticSelectionProvider

            from huipi_cloud.modules.exam_grading.runtime import ScoringWorker

            selection_mode = os.getenv("RUN_EXAM_SELECTION_BROWSER") == "1"
            score_config = config.model_copy(update={
                "grading_strategy": "quote_selection" if selection_mode else "strict_offsets"
            })
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
        assert len(scan_files) == 1 and scan_files[0].status == "stored"
        assert scan_files[0].sha256 == hashlib.sha256(scan_pdf.read_bytes()).hexdigest()
        assert len(scan_pages) == 4 and all(p.identity_status == "unconfirmed" for p in scan_pages)
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
        if grading_task is not None:
            await asyncio.wait_for(grading_task, timeout=15)
        if worker_task is not None:
            await asyncio.wait_for(worker_task, timeout=15)
        if parse_executor is not None:
            await parse_executor.aclose()
        async with ctx.factory() as db, db.begin():
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
