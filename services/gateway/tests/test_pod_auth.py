"""Workspace-agent pod auth: every :9000 call carries X-Pod-Token.

The workspace agent requires X-Pod-Token (the workspace's agent_token,
returned by the control-plane routing endpoint) on every endpoint except
/health. The gateway must present it on the paseo-password fetch and the
dev-server proxy, and must refuse the pod call entirely when routing
returns no agent_token (legacy workspaces) rather than send an
unauthenticated request.
"""

import main
from conftest import CLUSTER_IP


async def test_password_fetch_carries_pod_token(client, requests_log):
    pw = await main._fetch_paseo_password(CLUSTER_IP, "tok-alice")
    assert pw == "paseo-secret"
    pod = [r for r in requests_log if r.url.host == CLUSTER_IP and r.url.port == 9000]
    assert len(pod) == 1
    assert str(pod[0].url) == f"http://{CLUSTER_IP}:9000/password"
    assert pod[0].headers.get("x-pod-token") == "tok-alice"


async def test_dev_proxy_carries_pod_token(client, valid_cookie, requests_log):
    resp = await client.get("/workspace/dev/3000/app")
    assert resp.status_code == 200
    pod = [r for r in requests_log if r.url.host == CLUSTER_IP and r.url.port == 9000]
    assert len(pod) == 1
    assert str(pod[0].url) == f"http://{CLUSTER_IP}:9000/agent/exposures/exp-1/proxy/app"
    assert pod[0].headers.get("x-pod-token") == "tok-alice"


async def test_dev_proxy_refused_without_agent_token(client, valid_cookie, routing_config, requests_log):
    routing_config["agent_token"] = ""
    resp = await client.get("/workspace/dev/3000/app")
    assert resp.status_code == 503
    assert "not authenticated" in resp.text
    assert not [r for r in requests_log if r.url.host == CLUSTER_IP]


async def test_password_fetch_refused_without_agent_token(client, requests_log):
    pw = await main._fetch_paseo_password(CLUSTER_IP, "")
    assert pw == ""
    assert not [r for r in requests_log if r.url.host == CLUSTER_IP]
