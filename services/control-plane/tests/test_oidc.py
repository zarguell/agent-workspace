"""OIDC SSO tests against a mocked identity provider (authlib + joserfc).

The mock IdP serves discovery/JWKS/token over httpx.MockTransport injected
via oidc._transport; ID tokens are genuinely RS256-signed with a real RSA
key so signature verification is exercised end to end.
"""

import time
import urllib.parse
from uuid import uuid4

import httpx
import pytest_asyncio
from conftest import seed_user

ISSUER = "https://idp.test"
CLIENT_ID = "cid"
REDIRECT_URI = "http://test/api/oidc/callback"


@pytest_asyncio.fixture
async def oidc_enabled(monkeypatch):
    import oidc
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from joserfc import jwt as jose_jwt
    from joserfc.jwk import RSAKey

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    rsa_key = RSAKey.import_key(pem)
    pub_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_jwk = RSAKey.import_key(pub_pem).as_dict()
    public_jwk["kid"] = "test-kid"

    DISCOVERY = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/auth",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }

    holder = {"id_token": "not-set"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=DISCOVERY)
        if path.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [public_jwk]})
        if path.endswith("/token"):
            body = urllib.parse.parse_qs(request.content.decode())
            assert "code_verifier" in body, "PKCE code_verifier must be sent"
            assert body["code"][0] == "the-code"
            return httpx.Response(200, json={
                "id_token": holder["id_token"],
                "access_token": "access-token-1",
            })
        return httpx.Response(404, json={"error": "not found"})

    monkeypatch.setattr(oidc, "OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(oidc, "OIDC_DISCOVERY_URL", f"{ISSUER}/.well-known/openid-configuration")
    monkeypatch.setattr(oidc, "OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(oidc, "OIDC_CLIENT_SECRET", "cs")
    monkeypatch.setattr(oidc, "OIDC_REDIRECT_URI", REDIRECT_URI)
    monkeypatch.setattr(oidc, "OIDC_ADMIN_EMAILS", set())
    monkeypatch.setattr(oidc, "ENABLED", True)
    monkeypatch.setattr(oidc, "_transport", httpx.MockTransport(handler))

    def make_id_token(claims):
        return jose_jwt.encode({"alg": "RS256", "kid": "test-kid"}, claims, rsa_key)

    oidc_enabled.holder = holder
    oidc_enabled.make_id_token = make_id_token
    oidc_enabled.DISCOVERY = DISCOVERY
    return oidc_enabled


async def _start_flow(client):
    """Run /api/oidc/login and return (state, nonce)."""
    resp = await client.get("/api/oidc/login")
    assert resp.status_code == 302
    loc = resp.headers["location"]
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query))
    assert params["client_id"] == CLIENT_ID
    assert params["code_challenge"] and params["code_challenge_method"] == "S256"
    assert params["state"]
    import oidc
    return params["state"], oidc._state_store[params["state"]]["nonce"]

def _claims(sub=None, nonce="n", aud=CLIENT_ID, email="bob@example.com", **kw):
    now = int(time.time())
    claims = {
        "iss": ISSUER, "sub": sub or f"sub-{uuid4().hex[:10]}", "aud": aud, "nonce": nonce,
        "exp": now + 300, "iat": now, "email": email, "name": "Bob",
    }
    claims.update(kw)
    return claims


async def test_disabled_login_returns_404(client):
    resp = await client.get("/api/oidc/login")
    assert resp.status_code == 404
    assert (await client.get("/api/oidc/config")).json()["enabled"] is False


async def test_login_builds_pkce_redirect(client, oidc_enabled):
    state, nonce = await _start_flow(client)
    assert state and nonce  # state stored server-side


async def test_full_flow_provisions_user_and_session(client, oidc_enabled):
    state, nonce = await _start_flow(client)
    oidc_enabled.holder["id_token"] = oidc_enabled.make_id_token(
        _claims(nonce=nonce, email="fullflow@example.com"),
    )

    cb = await client.get(f"/api/oidc/callback?code=the-code&state={state}")
    assert cb.status_code == 302
    assert cb.headers["location"].endswith("/ui/workspaces")
    assert "session=" in cb.headers.get("set-cookie", "")

    # Session is valid and the workspace was auto-created.
    sess = await client.get("/api/session")
    assert sess.status_code == 200
    assert sess.json()["username"] == "fullflow"

    ws = await client.get("/api/workspaces")
    assert [w["workspace_id"] for w in ws.json()] == ["ws-fullflow"]


async def test_second_login_reuses_user(client, oidc_enabled):
    state, nonce = await _start_flow(client)
    oidc_enabled.holder["id_token"] = oidc_enabled.make_id_token(
        _claims(nonce=nonce, sub="reuse-sub", email="reuse@example.com"),
    )
    await client.get(f"/api/oidc/callback?code=the-code&state={state}")

    state2, nonce2 = await _start_flow(client)
    oidc_enabled.holder["id_token"] = oidc_enabled.make_id_token(
        _claims(nonce=nonce2, sub="reuse-sub", email="reuse@example.com"),
    )
    cb2 = await client.get(f"/api/oidc/callback?code=the-code&state={state2}")
    assert cb2.status_code == 302

    # Same sub -> still one user; session works.
    sess = await client.get("/api/session")
    assert sess.status_code == 200
    assert sess.json()["username"] == "reuse"


async def test_callback_invalid_state(client, oidc_enabled):
    resp = await client.get("/api/oidc/callback?code=the-code&state=bogus")
    assert resp.status_code == 400


async def test_callback_rejects_bad_signature(client, oidc_enabled):
    state, nonce = await _start_flow(client)
    # Sign with a different key entirely.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from joserfc import jwt as jose_jwt
    from joserfc.jwk import RSAKey
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )
    bad = jose_jwt.encode({"alg": "RS256", "kid": "test-kid"}, _claims(nonce=nonce), RSAKey.import_key(other_pem))
    oidc_enabled.holder["id_token"] = bad

    resp = await client.get(f"/api/oidc/callback?code=the-code&state={state}")
    assert resp.status_code == 401


async def test_key_rotation_refreshes_jwks(client, oidc_enabled):
    """A stale cached JWKS must be refreshed once when the provider rotates."""
    import oidc
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from joserfc.jwk import RSAKey

    # Preset the cache with an OLD key; the handler serves the NEW key.
    old = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    old_pub = old.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    old_jwk = RSAKey.import_key(old_pub).as_dict()
    old_jwk["kid"] = "test-kid"
    oidc._jwks_cache = {"keys": [old_jwk]}

    state, nonce = await _start_flow(client)
    oidc_enabled.holder["id_token"] = oidc_enabled.make_id_token(
        _claims(nonce=nonce, sub="rotated-sub", email="rotated@example.com"),
    )
    resp = await client.get(f"/api/oidc/callback?code=the-code&state={state}")
    assert resp.status_code == 302


async def test_callback_rejects_wrong_audience(client, oidc_enabled):
    state, nonce = await _start_flow(client)
    oidc_enabled.holder["id_token"] = oidc_enabled.make_id_token(_claims(nonce=nonce, aud="someone-else"))
    resp = await client.get(f"/api/oidc/callback?code=the-code&state={state}")
    assert resp.status_code == 401


async def test_callback_rejects_expired_token(client, oidc_enabled):
    state, nonce = await _start_flow(client)
    oidc_enabled.holder["id_token"] = oidc_enabled.make_id_token(_claims(nonce=nonce, exp=int(time.time()) - 60))
    resp = await client.get(f"/api/oidc/callback?code=the-code&state={state}")
    assert resp.status_code == 401


async def test_callback_rejects_nonce_mismatch(client, oidc_enabled):
    state, _ = await _start_flow(client)
    oidc_enabled.holder["id_token"] = oidc_enabled.make_id_token(_claims(nonce="wrong-nonce"))
    resp = await client.get(f"/api/oidc/callback?code=the-code&state={state}")
    assert resp.status_code == 401


async def test_provider_error_passthrough(client, oidc_enabled):
    resp = await client.get("/api/oidc/callback?error=access_denied&error_description=no")
    assert resp.status_code == 400
    assert "no" in resp.json()["error"]


async def test_admin_email_provisions_admin(client, oidc_enabled, monkeypatch):
    import oidc
    monkeypatch.setattr(oidc, "OIDC_ADMIN_EMAILS", {"admin@example.com"})
    state, nonce = await _start_flow(client)
    oidc_enabled.holder["id_token"] = oidc_enabled.make_id_token(
        _claims(nonce=nonce, sub="admin-sub", email="admin@example.com"),
    )

    await client.get(f"/api/oidc/callback?code=the-code&state={state}")
    sess = await client.get("/api/session")
    assert sess.json()["is_admin"] is True


async def test_username_collision_returns_409(client, oidc_enabled):
    # A local user already owns "bob".
    await seed_user("bob")
    state, nonce = await _start_flow(client)
    oidc_enabled.holder["id_token"] = oidc_enabled.make_id_token(_claims(nonce=nonce, sub="different-sub"))

    resp = await client.get(f"/api/oidc/callback?code=the-code&state={state}")
    assert resp.status_code == 409
