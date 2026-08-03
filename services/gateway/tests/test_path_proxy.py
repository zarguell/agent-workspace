"""Path-based proxying: /canvas/ and /code/ through the mocked control plane."""

from conftest import CLUSTER_IP


async def test_canvas_proxies_and_rewrites_assets(client, valid_cookie):
    resp = await client.get("/canvas/")
    assert resp.status_code == 200
    html = resp.text
    # base tag injected into <head>
    assert '<base href="/canvas/">' in html
    # root-relative asset URL rewritten to carry the prefix
    assert 'src="/canvas/assets/app.js"' in html


async def test_canvas_strips_prefix_on_upstream_call(client, valid_cookie, requests_log):
    await client.get("/canvas/")
    upstream = [r for r in requests_log if r.url.host == CLUSTER_IP]
    assert upstream, "expected an upstream proxy call"
    assert str(upstream[0].url) == f"http://{CLUSTER_IP}:8000/"
    assert "/canvas" not in str(upstream[0].url)


async def test_canvas_subpath_preserved(client, valid_cookie, requests_log):
    await client.get("/canvas/some/deep/path?q=1")
    upstream = [r for r in requests_log if r.url.host == CLUSTER_IP]
    assert str(upstream[0].url) == f"http://{CLUSTER_IP}:8000/some/deep/path?q=1"


async def test_canvas_hibernated_shows_starting_page(client, valid_cookie, routing_config, requests_log):
    routing_config.update(state="hibernated", agent_ready=False)
    resp = await client.get("/canvas/")
    assert resp.status_code == 200
    assert "Your workspace is starting" in resp.text
    # trigger_start was fired against the control plane
    start_calls = [
        r for r in requests_log
        if r.method == "POST" and r.url.path.startswith("/api/workspaces/") and r.url.path.endswith("/start")
    ]
    assert len(start_calls) == 1
    assert start_calls[0].url.path == "/api/workspaces/ws-alice/start"
    # no upstream proxy call while hibernated
    assert not [r for r in requests_log if r.url.host == CLUSTER_IP]


async def test_canvas_running_but_not_ready(client, valid_cookie, routing_config, requests_log):
    routing_config.update(state="running", agent_ready=False)
    resp = await client.get("/canvas/")
    # Not ready + not starting → terminal error page, never proxied
    assert resp.status_code == 503
    assert "Workspace Unavailable" in resp.text
    assert not [r for r in requests_log if r.url.host == CLUSTER_IP]


async def test_canvas_no_cluster_ip(client, valid_cookie, routing_config):
    routing_config.update(state="running", agent_ready=True, cluster_ip=None)
    resp = await client.get("/canvas/")
    assert resp.status_code == 503
    assert "Workspace has no network endpoint." in resp.text


async def test_code_proxy_passthrough(client, valid_cookie, requests_log):
    resp = await client.get("/code/")
    assert resp.status_code == 200
    upstream = [r for r in requests_log if r.url.host == CLUSTER_IP]
    assert str(upstream[0].url) == f"http://{CLUSTER_IP}:8080/"
