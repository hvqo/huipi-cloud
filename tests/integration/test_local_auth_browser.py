"""Optional Chromium test against actual local authentication and isolated PostgreSQL."""

import asyncio
import os
import signal
import socket
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from huipi_cloud.core.config import settings
from huipi_cloud.infrastructure.database import session as database
from huipi_cloud.infrastructure.database.session import get_db_session
from huipi_cloud.main import app
from huipi_cloud.modules.identity import dependencies, local_auth, mail
from huipi_cloud.modules.identity.models import (
    LocalAuthThrottle,
    LocalCredential,
    LocalPasswordReset,
    LocalSession,
    User,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.anyio
@pytest.mark.skipif(os.getenv("RUN_LOCAL_AUTH_BROWSER") != "1", reason="browser opt-in")
async def test_real_local_login_and_admin_flow(postgres_engine, monkeypatch):
    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    monkeypatch.setattr(database, "session_factory", factory)
    monkeypatch.setattr(dependencies, "verifier", local_auth.LocalSessionVerifier())
    previous = app.dependency_overrides.copy()

    async def db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    app.dependency_overrides[dependencies.get_auth_session] = db
    app.dependency_overrides.pop(dependencies.get_current_principal, None)
    app.dependency_overrides.pop(dependencies.authorize_request, None)
    api_socket = socket.socket()
    api_socket.bind(("127.0.0.1", 0))
    api_port = api_socket.getsockname()[1]
    control_socket = socket.socket()
    control_socket.bind(("127.0.0.1", 0))
    control_port = control_socket.getsockname()[1]
    port_socket = socket.socket()
    port_socket.bind(("127.0.0.1", 0))
    frontend_port = port_socket.getsockname()[1]
    port_socket.close()
    origin = f"http://127.0.0.1:{frontend_port}"
    monkeypatch.setattr(settings, "auth_allowed_origins", origin)
    delivered = {}
    monkeypatch.setattr(mail, "configured", lambda: True)

    async def capture_mail(email, token, purpose):
        delivered[purpose] = {"email": email, "token": token}

    monkeypatch.setattr(mail, "send", capture_mail)
    control = FastAPI()

    @control.get("/mail/{purpose}")
    async def sent_mail(purpose: str):
        if purpose not in delivered:
            raise HTTPException(404)
        return delivered[purpose]

    suffix = uuid4().hex
    admin_account = f"admin-{suffix}@example.invalid"
    teacher_account = f"teacher-{suffix}@example.invalid"
    password = "synthetic-admin-password-2026!"
    async with factory() as session, session.begin():
        admin = User(issuer="huipi:local", subject=str(uuid4()), is_admin=True, is_active=True)
        session.add(admin)
        await session.flush()
        session.add(
            LocalCredential(
                user_id=admin.id,
                login_name=admin_account,
                display_name="合成管理员",
                password_hash=await local_auth.hash_password(password),
                must_change_password=False,
            )
        )
        admin_id = admin.id
    server = uvicorn.Server(
        uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning")
    )
    api_task = asyncio.create_task(server.serve(sockets=[api_socket]))
    control_server = uvicorn.Server(
        uvicorn.Config(control, lifespan="off", access_log=False, log_level="warning")
    )
    control_task = asyncio.create_task(control_server.serve(sockets=[control_socket]))
    environment = dict(
        os.environ,
        WORKBENCH_PORT=str(frontend_port),
        WORKBENCH_API_TARGET=f"http://127.0.0.1:{api_port}",
    )
    vite = await asyncio.create_subprocess_exec(
        "npm",
        "run",
        "dev",
        cwd=ROOT / "frontend",
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with httpx.AsyncClient(trust_env=False) as http, asyncio.timeout(30):
            while True:
                try:
                    if (await http.get(origin)).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        browser_env = dict(
            environment,
            WORKBENCH_URL=origin,
            RUN_LOCAL_AUTH_BROWSER="1",
            LOCAL_AUTH_ADMIN=admin_account,
            LOCAL_AUTH_TEACHER=teacher_account,
            LOCAL_AUTH_ADMIN_PASSWORD=password,
            LOCAL_AUTH_CONTROL_URL=f"http://127.0.0.1:{control_port}",
        )
        process = await asyncio.create_subprocess_exec(
            "npm",
            "exec",
            "playwright",
            "test",
            "e2e/local-auth.spec.ts",
            cwd=ROOT / "frontend",
            env=browser_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async with asyncio.timeout(120):
            output, _ = await process.communicate()
        assert process.returncode == 0, output.decode(errors="replace")[-12000:]
    finally:
        try:
            os.killpg(vite.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        await vite.wait()
        server.should_exit = True
        control_server.should_exit = True
        await asyncio.wait_for(asyncio.gather(api_task, control_task), 10)
        api_socket.close()
        control_socket.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        async with factory() as session, session.begin():
            ids = list(
                (
                    await session.scalars(
                        select(LocalCredential.user_id).where(
                            LocalCredential.login_name.in_([admin_account, teacher_account])
                        )
                    )
                ).all()
            )
            if admin_id not in ids:
                ids.append(admin_id)
            await session.execute(delete(LocalSession).where(LocalSession.user_id.in_(ids)))
            await session.execute(
                delete(LocalPasswordReset).where(LocalPasswordReset.user_id.in_(ids))
            )
            await session.execute(delete(LocalCredential).where(LocalCredential.user_id.in_(ids)))
            await session.execute(text("TRUNCATE TABLE local_auth_audit"))
            await session.execute(delete(User).where(User.id.in_(ids)))
            await session.execute(delete(LocalAuthThrottle))
