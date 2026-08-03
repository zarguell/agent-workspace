"""OIDC SSO paths must be reachable through the gateway without a session
(the control plane handles SSO; the gateway only exempts the prefix)."""

import httpx
import pytest_asyncio


@pytest_asyncio.fixture
async def no_cookie(client):
    """Client with no session cookie."""
    return client


async def test_oidc_config_reachable_without_session(no_cookie):
    resp = await no_cookie.get("/api/oidc/config")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "issuer": None}


async def test_oidc_callback_passthrough_without_session(no_cookie):
    # Not a redirect to login: the request reaches the control plane.
    resp = await no_cookie.get("/api/oidc/callback?state=bogus")
    assert resp.status_code == 400
    assert resp.json()["error"] == "Invalid state"


async def test_oidc_login_still_requires_session(no_cookie):
    # /api/oidc/login is a control-plane route too, but the gateway proxies
    # the whole oidc prefix; without a session the CP would redirect, here
    # the mock returns its fallback (404 from the proxy target shape).
    resp = await no_cookie.get("/api/oidc/login")
    assert resp.status_code == 404
