"""Set-Cookie forwarding: the Domain attribute is appended only when
COOKIE_DOMAIN is set to a non-empty value; otherwise the upstream
Set-Cookie is forwarded verbatim (never a foreign placeholder domain)."""

from conftest import _login_config, _user_config

LOGIN_COOKIE = "session=abc; Path=/; HttpOnly"
USER_COOKIE = "workspace=ws-alice; Path=/; HttpOnly"


async def test_login_cookie_forwarded_verbatim_when_domain_unset(client, monkeypatch):
    monkeypatch.delenv("COOKIE_DOMAIN", raising=False)
    resp = await client.post("/api/login", json={"username": "alice"})
    assert resp.status_code == 200
    assert resp.headers["set-cookie"] == LOGIN_COOKIE


async def test_login_cookie_forwarded_verbatim_when_domain_empty(client, monkeypatch):
    monkeypatch.setenv("COOKIE_DOMAIN", "")
    resp = await client.post("/api/login", json={"username": "alice"})
    assert resp.headers["set-cookie"] == LOGIN_COOKIE


async def test_login_cookie_gains_domain_when_configured(client, monkeypatch):
    monkeypatch.setenv("COOKIE_DOMAIN", ".example.com")
    resp = await client.post("/api/login", json={"username": "alice"})
    assert resp.headers["set-cookie"] == LOGIN_COOKIE + "; Domain=.example.com"


async def test_login_cookie_domain_not_duplicated(client, monkeypatch):
    monkeypatch.setenv("COOKIE_DOMAIN", ".example.com")
    _login_config["set_cookie"] = "session=abc; Domain=internal.test; Path=/"
    resp = await client.post("/api/login", json={"username": "alice"})
    assert resp.headers["set-cookie"] == "session=abc; Domain=internal.test; Path=/"


async def test_api_proxy_cookie_forwarded_verbatim_when_domain_unset(client, valid_cookie, monkeypatch):
    monkeypatch.delenv("COOKIE_DOMAIN", raising=False)
    resp = await client.get("/api/user")
    assert resp.status_code == 200
    assert resp.headers["set-cookie"] == USER_COOKIE


async def test_api_proxy_cookie_gains_domain_when_configured(client, valid_cookie, monkeypatch):
    monkeypatch.setenv("COOKIE_DOMAIN", ".example.com")
    resp = await client.get("/api/user")
    assert resp.headers["set-cookie"] == USER_COOKIE + "; Domain=.example.com"
