"""Actual RS256 signatures and async HTTPX JWKS transport, never an external IdP."""

import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from huipi_cloud.core.config import Settings
from huipi_cloud.infrastructure.auth.jwt import TokenVerifier
from huipi_cloud.modules.identity.protocol import AccessError


@pytest.fixture(scope="session")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(key, kid="test-key"):
    return {
        **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())),
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
    }


def config():
    return Settings(
        _env_file=None,
        auth_issuer="https://idp.invalid",
        auth_audience="huipi-api",
        auth_jwks_url="https://idp.invalid/jwks",
    )


def token(key, *, claims=None, headers=None):
    now = int(time.time())
    payload = {
        "iss": "https://idp.invalid",
        "aud": "huipi-api",
        "sub": "teacher-a",
        "iat": now - 1,
        "exp": now + 3600,
        **(claims or {}),
    }
    return jwt.encode(
        payload, key, algorithm="RS256", headers={"kid": "test-key", **(headers or {})}
    )


def verifier(key, **kwargs):
    return TokenVerifier(
        config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"keys": [jwk(key)]}),
        ),
        **kwargs,
    )


@pytest.mark.anyio
async def test_real_signature_claims_and_no_roles_in_verified_identity(signing_key):
    identity = await verifier(signing_key).verify(
        token(
            signing_key,
            claims={
                "teacher_id": "somebody-else",
                "role": "admin",
            },
        )
    )
    assert identity.subject == "teacher-a"
    assert not hasattr(identity, "role") and not hasattr(identity, "teacher_id")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://evil.invalid"},
        {"aud": "another-api"},
        {"exp": 1},
        {"iat": int(time.time()) + 3600},
        {"nbf": int(time.time()) + 3600},
        {"exp": float("inf")},
        {"iat": True},
        {"sub": ""},
        {"sub": 123},
    ],
)
async def test_invalid_registered_claims_rejected(signing_key, claims):
    with pytest.raises(AccessError) as caught:
        await verifier(signing_key).verify(token(signing_key, claims=claims))
    assert caught.value.code == "invalid_token"


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["exp", "iat", "iss", "aud", "sub"])
async def test_required_claims(signing_key, field):
    payload = jwt.decode(token(signing_key), options={"verify_signature": False})
    del payload[field]
    bad = jwt.encode(payload, signing_key, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(AccessError):
        await verifier(signing_key).verify(bad)


@pytest.mark.anyio
async def test_invalid_signature_none_and_hmac_algorithms(signing_key):
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad = [
        token(wrong_key),
        jwt.encode({"sub": "x"}, "", algorithm="none"),
        jwt.encode({"sub": "x"}, "synthetic-only" * 4, algorithm="HS256"),
        "bad.jwt",
    ]
    for value in bad:
        with pytest.raises(AccessError):
            await verifier(signing_key).verify(value)


@pytest.mark.anyio
async def test_jwks_cache_rotation_unknown_kid_and_no_token_endpoint_influence(signing_key):
    now = [0.0]
    key2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    response_keys = [jwk(signing_key)]
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"keys": response_keys})

    check = TokenVerifier(config(), transport=httpx.MockTransport(handler), clock=lambda: now[0])
    first = token(signing_key, headers={"jku": "https://evil.invalid/key"})
    await check.verify(first)
    await check.verify(first)
    assert requests == ["https://idp.invalid/jwks"]
    for _ in range(3):
        with pytest.raises(AccessError):
            await check.verify(token(key2, headers={"kid": "new-key"}))
    assert len(requests) == 1
    now[0] = 11
    response_keys[:] = [jwk(key2, "new-key")]
    await check.verify(token(key2, headers={"kid": "new-key"}))
    assert len(requests) == 2
    with pytest.raises(AccessError):
        await check.verify(first)  # Removed key is not retained across refresh.


@pytest.mark.anyio
async def test_expired_cache_does_not_fall_back_to_stale_keys(signing_key):
    now = [0.0]

    def handler(request):
        return (
            httpx.Response(503)
            if now[0]
            else httpx.Response(200, json={"keys": [jwk(signing_key)]})
        )

    check = TokenVerifier(config(), transport=httpx.MockTransport(handler), clock=lambda: now[0])
    await check.verify(token(signing_key))
    now[0] = 301
    with pytest.raises(AccessError) as caught:
        await check.verify(token(signing_key))
    assert caught.value.status_code == 503


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [{"keys": []}, {"keys": ["invalid"]}, {"keys": [{"kid": "x", "kty": "RSA", "n": "bad"}]}],
)
async def test_invalid_jwks_is_safe_unavailable(signing_key, body):
    check = TokenVerifier(
        config(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body),
        ),
    )
    with pytest.raises(AccessError) as caught:
        await check.verify(token(signing_key))
    assert caught.value.code == "identity_service_unavailable"


@pytest.mark.parametrize(
    "field,value",
    [
        ("auth_jwks_url", "http://idp.invalid/keys"),
        ("auth_jwks_url", "https://user:secret@idp.invalid/keys"),
        ("auth_issuer", "https://idp.invalid/#bad"),
    ],
)
def test_issuer_key_url_server_configuration_is_restricted(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_http_permission_inventory_matches_openapi_and_exact_fixed_policy():
    from pathlib import Path

    from huipi_cloud.main import app
    from huipi_cloud.modules.identity.dependencies import POLICIES, POLICY_PATHS

    root = Path(__file__).resolve().parents[2]
    inventory = json.loads((root / "docs/security/http-permissions.json").read_text())
    rows = inventory["operations"]
    assert len({(r["method"], r["path"]) for r in rows}) == len(rows)
    actual = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "delete"}
    }
    documented = {
        (row["method"], row["path"]) for row in rows if row["path"].startswith("/api/v1/")
    }
    assert actual == documented
    assert set(POLICIES) == set(POLICY_PATHS)
    for row in rows:
        if row["permission"] == "public":
            continue
        assert row["method"] in POLICIES[row["policy"]]
        assert row["path"] == "/api/v1" + POLICY_PATHS[row["policy"]]
        assert row["permission"] != "public"
    public_documentation = [row for row in rows if row["policy"] == "public_documentation"]
    assert len(public_documentation) == 8


@pytest.mark.anyio
async def test_missing_identity_configuration_never_enables_anonymous_access():
    check = TokenVerifier(
        Settings(
            _env_file=None,
            auth_issuer=None,
            auth_audience=None,
            auth_jwks_url=None,
        )
    )
    with pytest.raises(AccessError) as caught:
        await check.verify("not-a-token")
    assert caught.value.code == "authentication_not_configured"
    assert caught.value.status_code == 503


@pytest.mark.anyio
async def test_jwks_network_timeout_and_oversized_response_fail_closed(signing_key):
    def timeout(request):
        raise httpx.ReadTimeout("synthetic upstream timeout")

    checks = [
        TokenVerifier(config(), transport=httpx.MockTransport(timeout)),
        TokenVerifier(
            config(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b" " * 65537),
            ),
        ),
    ]
    for check in checks:
        with pytest.raises(AccessError) as caught:
            await check.verify(token(signing_key))
        assert caught.value.code == "identity_service_unavailable"
