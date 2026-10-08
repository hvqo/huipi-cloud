"""Tests for the minimal health endpoint."""

import asyncio

import httpx

from huipi_cloud.main import app


def test_health_returns_ok() -> None:
    response = asyncio.run(_get_health())
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def _get_health() -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/api/v1/health")
