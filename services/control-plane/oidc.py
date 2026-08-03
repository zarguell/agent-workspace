"""Generic OpenID Connect (OIDC) SSO for the control plane.

Protocol handled by Authlib's AsyncOAuth2Client (authorization-code + PKCE
S256, token exchange); ID-token verification by joserfc (JWKS signature,
algorithm allowlist, expiry, issuer/audience/nonce). Works with any
provider that supports OIDC discovery — Google, Microsoft Entra, Keycloak,
Auth0, Okta, GitHub, ...

Enabled by setting OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET and
OIDC_REDIRECT_URI. First login provisions a user (keyed by the provider's
``sub`` claim); users whose email matches OIDC_ADMIN_EMAILS become admins.
"""

import logging
import os
import secrets
import time

from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from joserfc import jwt as jose_jwt
from joserfc.errors import BadSignatureError, InvalidKeyIdError
from joserfc.jwk import KeySet
from sqlalchemy import select

from auth import create_session, make_session_cookie
from database import async_session_factory
from models import User, Workspace

logger = logging.getLogger("control-plane.oidc")

OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI", "")
OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid profile email")
OIDC_ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("OIDC_ADMIN_EMAILS", "").split(",") if e.strip()}
# Optional override for the discovery fetch. Normally derived from
# OIDC_ISSUER; set it when the browser-facing issuer differs from the URL
# this container can reach (e.g. a mock IdP on the compose network).
OIDC_DISCOVERY_URL = (
    os.environ.get("OIDC_DISCOVERY_URL", "")
    or f"{OIDC_ISSUER}/.well-known/openid-configuration"
)

ENABLED = bool(OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET and OIDC_REDIRECT_URI)
ALLOWED_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]

router = APIRouter(prefix="/api/oidc", tags=["oidc"])

# Tests inject an httpx transport here (MockTransport simulating the IdP).
_transport = None

_discovery_cache: dict | None = None
_jwks_cache: dict | None = None
_state_store: dict[str, dict] = {}  # state -> {code_verifier, nonce, created_at}
STATE_TTL_SECONDS = 600


def _make_client() -> AsyncOAuth2Client:
    kwargs = {
        "client_id": OIDC_CLIENT_ID,
        "client_secret": OIDC_CLIENT_SECRET,
        "redirect_uri": OIDC_REDIRECT_URI,
        "scope": OIDC_SCOPES,
        "code_challenge_method": "S256",
    }
    if _transport is not None:
        kwargs["transport"] = _transport
    return AsyncOAuth2Client(**kwargs)


async def _discovery() -> dict:
    """Fetch (and cache) the provider's OIDC discovery document."""
    global _discovery_cache
    if _discovery_cache is None:
        async with _make_client() as client:
            resp = await client.request(
                "GET",
                OIDC_DISCOVERY_URL,
                withhold_token=True,
            )
            resp.raise_for_status()
            _discovery_cache = resp.json()
    return _discovery_cache

async def _fetch_jwks(meta: dict) -> dict:
    async with _make_client() as client:
        resp = await client.request("GET", meta["jwks_uri"], withhold_token=True)
        resp.raise_for_status()
        return resp.json()


async def _verify_id_token(id_token: str, expected_nonce: str) -> dict:
    """Verify an ID token: signature (JWKS), alg allowlist, exp, iss, aud, nonce.

    joserfc's decode validates the signature and algorithm allowlist;
    issuer/audience/nonce are checked explicitly here, as is time-based
    validity (exp/nbf), which joserfc's decode does not do.
    """
    global _jwks_cache
    meta = await _discovery()
    if _jwks_cache is None:
        _jwks_cache = await _fetch_jwks(meta)

    try:
        return _verify_claims(id_token, _jwks_cache, expected_nonce)
    except (BadSignatureError, InvalidKeyIdError):
        # The provider rotated its signing keys and our cached JWKS is
        # stale; refresh once and retry before giving up.
        _jwks_cache = await _fetch_jwks(meta)
        return _verify_claims(id_token, _jwks_cache, expected_nonce)


def _verify_claims(id_token: str, jwks: dict, expected_nonce: str) -> dict:
    """Verify an ID token against a JWKS: signature, alg allowlist, exp,
    iss, aud, nonce.

    joserfc's decode validates the signature and algorithm allowlist;
    issuer/audience/nonce are checked explicitly here, as is time-based
    validity (exp/nbf), which joserfc's decode does not do.
    """
    key_set = KeySet.import_key_set(jwks)
    token = jose_jwt.decode(id_token, key_set, algorithms=ALLOWED_ALGS)
    claims = token.claims

    # joserfc's decode verifies the signature + algorithm allowlist only;
    # time-based validity (exp/nbf) is checked here per the OIDC spec.
    now = int(time.time())
    exp = claims.get("exp")
    if not isinstance(exp, int) or exp < now - 30:
        raise ValueError("token expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, int) and nbf > now + 30:
        raise ValueError("token not yet valid")

    if claims.get("iss") != OIDC_ISSUER:
        raise ValueError("issuer mismatch")
    aud = claims.get("aud")
    if OIDC_CLIENT_ID not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("audience mismatch")
    if expected_nonce and claims.get("nonce") != expected_nonce:
        raise ValueError("nonce mismatch")
    return claims


def _prune_state():
    now = time.monotonic()
    expired = [s for s, p in _state_store.items() if now - p["created_at"] > STATE_TTL_SECONDS]
    for s in expired:
        _state_store.pop(s, None)


@router.get("/config")
async def oidc_config():
    """Whether SSO is enabled (used by the login page)."""
    return {"enabled": ENABLED, "issuer": OIDC_ISSUER if ENABLED else None}


@router.get("/login")
async def oidc_login():
    """Start the authorization-code + PKCE flow: redirect to the provider."""
    if not ENABLED:
        return JSONResponse(status_code=404, content={"error": "OIDC not configured"})
    meta = await _discovery()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(16)
    code_verifier = secrets.token_urlsafe(48)
    client = _make_client()
    async with client:
        url, _ = client.create_authorization_url(
            meta["authorization_endpoint"],
            state=state,
            code_verifier=code_verifier,
            nonce=nonce,
        )
    _state_store[state] = {"code_verifier": code_verifier, "nonce": nonce, "created_at": time.monotonic()}
    _prune_state()
    return RedirectResponse(url=url, status_code=302)


@router.get("/callback")
async def oidc_callback(request: Request):
    """Complete the flow: exchange the code, verify the ID token, log in."""
    if not ENABLED:
        return JSONResponse(status_code=404, content={"error": "OIDC not configured"})

    params = request.query_params
    if params.get("error"):
        return JSONResponse(status_code=400, content={"error": params.get("error_description") or "OIDC error"})
    code = params.get("code", "")
    state = params.get("state", "")
    pending = _state_store.pop(state, None)
    if pending is None:
        return JSONResponse(status_code=400, content={"error": "Invalid state"})
    if time.monotonic() - pending["created_at"] > STATE_TTL_SECONDS:
        return JSONResponse(status_code=400, content={"error": "State expired"})

    try:
        meta = await _discovery()
        client = _make_client()
        async with client:
            token = await client.fetch_token(
                meta["token_endpoint"],
                code=code,
                code_verifier=pending["code_verifier"],
            )
        id_token = token.get("id_token")
        if not id_token:
            raise ValueError("no id_token in token response")
        claims = await _verify_id_token(id_token, pending["nonce"])
    except Exception as e:
        logger.warning("OIDC callback rejected: %s", e)
        return JSONResponse(status_code=401, content={"error": "Authentication failed"})

    return await _provision_session(claims)


async def _provision_session(claims: dict):
    """Find or provision the platform user, then create a session."""
    sub = claims.get("sub", "")
    email = (claims.get("email") or "").lower()
    async with async_session_factory() as db:
        result = await db.execute(select(User).where(User.oidc_sub == sub))
        user = result.scalar_one_or_none()
        if user is None:
            username = (
                claims.get("preferred_username")
                or email.split("@")[0]
                or f"user-{sub[:8]}"
            )
            existing = await db.execute(select(User).where(User.username == username))
            if existing.scalar_one_or_none() is not None:
                return JSONResponse(status_code=409, content={
                    "error": f"Username '{username}' is already taken by another account",
                })
            user = User(
                username=username,
                password_hash="!",  # SSO users have no local password
                display_name=claims.get("name") or username,
                is_admin=bool(email and email in OIDC_ADMIN_EMAILS),
                oidc_sub=sub,
            )
            db.add(user)
            await db.flush()
            logger.info("Provisioned SSO user %s (sub=%s)", user.username, sub)

        # Auto-create the workspace, matching the local-login behavior.
        ws = await db.get(Workspace, f"ws-{user.username}")
        if ws is None:
            db.add(Workspace(
                workspace_id=f"ws-{user.username}",
                user_id=user.user_id,
                state="requested",
                image="",
            ))

        session = await create_session(db, user)
        await db.commit()

        resp = RedirectResponse(url="/ui/workspaces", status_code=302)
        resp.set_cookie(**make_session_cookie(session.session_id))
        return resp
