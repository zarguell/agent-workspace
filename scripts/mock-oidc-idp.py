#!/usr/bin/env python3
"""Minimal mock OIDC identity provider for local SSO testing.

Serves discovery, JWKS, an authorization form, and a token endpoint.
ID tokens are RS256-signed with a freshly generated RSA key via joserfc.
Any client_id is accepted; the audience echoes the requesting client.
The nonce, state and redirect_uri from the authorization request are
carried through hidden form fields; on sign-in the browser is redirected
to the client's redirect_uri with the authorization code.

Usage:
    python scripts/mock-oidc-idp.py --issuer http://10.44.122.208:18099 [--port 18099]
"""

import argparse
import json
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt as jose_jwt
from joserfc.jwk import RSAKey

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
RSA_KEY = RSAKey.import_key(PEM)
PUB_PEM = KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
)
PUBLIC_JWK = RSAKey.import_key(PUB_PEM).as_dict()
PUBLIC_JWK["kid"] = "mock-idp-kid"

ISSUER = ""  # set from --issuer
INTERNAL_BASE = ""  # set from --internal-base (defaults to ISSUER)
CODES = {}  # code -> {client_id, nonce, email, name, sub, created_at}


def make_discovery(issuer: str, internal_base: str) -> dict:
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/auth",
        "token_endpoint": f"{internal_base}/token",
        "jwks_uri": f"{internal_base}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "profile", "email"],
    }


def make_id_token(client_id: str, nonce: str, email: str, name: str, sub: str) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": sub,
        "aud": client_id,
        "nonce": nonce,
        "exp": now + 600,
        "iat": now,
        "email": email,
        "email_verified": True,
        "name": name,
        "preferred_username": email.split("@")[0],
    }
    return jose_jwt.encode({"alg": "RS256", "kid": "mock-idp-kid"}, claims, RSA_KEY)


async def app(scope, receive, send):
    assert scope["type"] == "http"
    path = scope["path"]
    method = scope["method"]

    if path == "/.well-known/openid-configuration" and method == "GET":
        return await _json(send, 200, make_discovery(ISSUER, INTERNAL_BASE))

    if path == "/jwks" and method == "GET":
        return await _json(send, 200, {"keys": [PUBLIC_JWK]})

    if path == "/token" and method == "POST":
        body = await _read_body(receive)
        params = urllib.parse.parse_qs(body.decode())
        code = params.get("code", [""])[0]
        entry = CODES.pop(code, None)
        if entry is None:
            return await _json(send, 400, {"error": "invalid_grant"})
        return await _json(send, 200, {
            "id_token": make_id_token(entry["client_id"], entry["nonce"], entry["email"], entry["name"], entry["sub"]),
            "access_token": secrets.token_urlsafe(32),
            "token_type": "Bearer",
            "expires_in": 3600,
        })

    if path == "/auth":
        qs = urllib.parse.parse_qs(scope.get("query_string", b"").decode())
        if method == "GET":
            redirect_uri = qs.get("redirect_uri", [""])[0]
            html = f"""<!doctype html><html><head><title>Mock IdP sign-in</title></head>
<body style="font-family:system-ui;max-width:360px;margin:80px auto">
<h2>Mock IdP</h2>
<form method="post" action="/auth">
<input type="hidden" name="state" value="{qs.get('state',[''])[0]}">
<input type="hidden" name="nonce" value="{qs.get('nonce',[''])[0]}">
<input type="hidden" name="client_id" value="{qs.get('client_id',[''])[0]}">
<input type="hidden" name="redirect_uri" value="{redirect_uri}">
<p><label>Email <input name="email" value="sso.user@example.com" style="width:100%"></label></p>
<p><label>Name <input name="name" value="SSO User" style="width:100%"></label></p>
<button style="width:100%;padding:8px">Sign in</button>
</form></body></html>"""
            return await _html(send, 200, html)
        if method == "POST":
            form = urllib.parse.parse_qs((await _read_body(receive)).decode())
            client_id = form.get("client_id", [""])[0] or qs.get("client_id", [""])[0]
            state = form.get("state", [""])[0]
            nonce = form.get("nonce", [""])[0]
            redirect_uri = form.get("redirect_uri", [""])[0] or qs.get("redirect_uri", [""])[0]
            email = form.get("email", ["sso.user@example.com"])[0]
            name = form.get("name", ["SSO User"])[0]
            code = secrets.token_urlsafe(24)
            CODES[code] = {
                "client_id": client_id,
                "nonce": nonce,
                "email": email,
                "name": name,
                # Real IdPs return a stable subject per user; derive ours
                # from the email so repeated sign-ins reuse the account.
                "sub": f"mock-{email.lower()}",
                "created_at": time.time(),
            }
            sep = "&" if "?" in redirect_uri else "?"
            await _redirect(send, f"{redirect_uri}{sep}code={code}&state={state}")
            return

    return await _json(send, 404, {"error": "not found"})


async def _read_body(receive):
    body = b""
    message = await receive()
    if message["type"] == "http.request":
        body += message.get("body", b"")
        if not message.get("more_body", False):
            return body
    elif message["type"] == "http.disconnect":
        return body


async def _send_headers(send, status, content_type, extra=None):
    headers = [(b"content-type", content_type.encode())]
    for k, v in (extra or {}).items():
        headers.append((k.encode(), v.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})


async def _json(send, status, payload, extra=None):
    data = json.dumps(payload).encode()
    await _send_headers(send, status, "application/json", extra)
    await send({"type": "http.response.body", "body": data})


async def _html(send, status, body):
    await _send_headers(send, status, "text/html")
    await send({"type": "http.response.body", "body": body.encode()})


async def _redirect(send, location):
    await _send_headers(send, 302, "text/plain", {"location": location})
    await send({"type": "http.response.body", "body": b"redirecting"})


def main():
    global ISSUER, INTERNAL_BASE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18099)
    parser.add_argument("--issuer", required=True, help="public base URL, e.g. http://127.0.0.1:18099")
    parser.add_argument(
        "--internal-base",
        default=None,
        help="base URL used for token/jwks endpoints (for clients inside the "
        "compose network, e.g. http://mock-idp:18099); defaults to --issuer",
    )
    args = parser.parse_args()
    ISSUER = args.issuer.rstrip("/")
    INTERNAL_BASE = (args.internal_base or ISSUER).rstrip("/")
    print(f"Mock IdP issuer: {ISSUER}", flush=True)
    print(f"Mock IdP internal base: {INTERNAL_BASE}", flush=True)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
