"""Bounded async JWKS cache. Only a configured HTTPS issuer/key endpoint is trusted."""

import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import jwt

from huipi_cloud.modules.identity.protocol import AccessError


@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    expires_at: datetime


class TokenVerifier:
    def __init__(self, config, *, transport=None, clock=time.monotonic):
        self.config = config
        self.transport = transport
        self.clock = clock
        self.keys = {}
        self.loaded_at = float("-inf")
        self.lock = asyncio.Lock()

    async def _refresh(self):
        try:
            async with httpx.AsyncClient(
                timeout=3.0,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                async with client.stream("GET", self.config.auth_jwks_url) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        if len(content) + len(chunk) > 65536:
                            raise ValueError
                        content.extend(chunk)
            payload = json.loads(content)
            raw_keys = payload.get("keys") if isinstance(payload, dict) else None
            if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= 16:
                raise ValueError
            keys = {}
            for item in raw_keys:
                if not isinstance(item, dict):
                    raise ValueError
                # Ignore unrelated encryption/other-algorithm keys; never infer the algorithm.
                if item.get("kty") != "RSA" or item.get("use", "sig") != "sig":
                    continue
                if item.get("alg", "RS256") != "RS256":
                    continue
                if "key_ops" in item and item["key_ops"] != ["verify"]:
                    continue
                kid = item.get("kid")
                if not isinstance(kid, str) or not 1 <= len(kid) <= 128 or kid in keys:
                    raise ValueError
                key = jwt.PyJWK.from_dict(item, algorithm="RS256").key
                if not 2048 <= key.key_size <= 8192:
                    raise ValueError
                keys[kid] = key
            if not keys:
                raise ValueError
        except (httpx.HTTPError, ValueError, jwt.PyJWTError, TypeError, OverflowError):
            raise AccessError("identity_service_unavailable", 503) from None
        self.keys = keys
        self.loaded_at = self.clock()

    async def verify(self, token: str) -> VerifiedIdentity:
        cfg = self.config
        if not (cfg.auth_issuer and cfg.auth_audience and cfg.auth_jwks_url):
            raise AccessError("authentication_not_configured", 503)
        if len(token) > 16384:
            raise AccessError("invalid_token", 401)
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or header.get("crit"):
                raise ValueError
            if not isinstance(kid, str) or not 1 <= len(kid) <= 128:
                raise ValueError
        except (jwt.PyJWTError, ValueError, TypeError):
            raise AccessError("invalid_token", 401) from None
        try:
            async with asyncio.timeout(5), self.lock:
                age = self.clock() - self.loaded_at
                # Unknown kid may refresh once per cooldown, preventing a random-kid fetch storm.
                if age >= cfg.auth_jwks_cache_seconds or (
                    kid not in self.keys and age >= cfg.auth_jwks_refresh_seconds
                ):
                    await self._refresh()
                key = self.keys.get(kid)
        except TimeoutError:
            raise AccessError("identity_service_unavailable", 503) from None
        if key is None:
            raise AccessError("invalid_token", 401)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=cfg.auth_audience,
                issuer=cfg.auth_issuer,
                leeway=cfg.auth_clock_skew_seconds,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )
            for field in ("exp", "iat", "nbf"):
                if field in claims and (
                    type(claims[field]) not in (int, float) or not math.isfinite(claims[field])
                ):
                    raise ValueError
            subject = claims["sub"]
            if not subject.strip() or len(subject) > 255 or claims["exp"] <= claims["iat"]:
                raise ValueError
            expires = datetime.fromtimestamp(claims["exp"], UTC)
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError, OSError):
            raise AccessError("invalid_token", 401) from None
        return VerifiedIdentity(cfg.auth_issuer, subject, expires)
