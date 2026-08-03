"""Activity liveness touch: fired after successful routing, throttled per
workspace, never fired for unauthenticated or wake/starting-page flows.
Also: idle_pending workspaces are routable like running ones."""

import asyncio
import json
import time

import main

ACTIVITY_PATH = "/api/internal/activity"


def _activity_posts(requests_log):
    return [r for r in requests_log if r.url.path == ACTIVITY_PATH]


async def _drain():
    # Let fire-and-forget background tasks (activity/audit) run to completion.
    await asyncio.sleep(0.05)


async def test_activity_touch_sent_after_successful_routing(client, valid_cookie, requests_log):
    resp = await client.get("/canvas/")
    assert resp.status_code == 200
    await _drain()

    posts = _activity_posts(requests_log)
    assert len(posts) == 1
    post = posts[0]
    assert post.method == "POST"
    assert post.headers.get("x-service-auth") == "test-token"
    assert post.headers.get("content-type") == "application/json"
    assert json.loads(post.read()) == {"workspace_id": "ws-alice"}


async def test_activity_touch_throttled_within_window(client, valid_cookie, requests_log):
    await client.get("/canvas/")
    await client.get("/canvas/")
    await _drain()
    assert len(_activity_posts(requests_log)) == 1


async def test_activity_touch_fires_after_throttle_window(client, valid_cookie, requests_log):
    await client.get("/canvas/")
    await _drain()
    # Backdate the recorded touch past the 60s window → next hit must fire.
    main._LAST_ACTIVITY_TOUCH["ws-alice"] = time.monotonic() - 120
    await client.get("/canvas/")
    await _drain()
    assert len(_activity_posts(requests_log)) == 2


async def test_activity_touch_not_sent_unauthenticated(client, requests_log):
    resp = await client.get("/canvas/")
    assert resp.status_code == 307  # redirected to login
    await _drain()
    assert _activity_posts(requests_log) == []


async def test_activity_touch_not_sent_for_wake_page(client, valid_cookie, requests_log, routing_config):
    routing_config.update(state="hibernated", agent_ready=False)
    resp = await client.get("/canvas/")
    assert resp.status_code == 200
    assert "starting" in resp.text.lower()  # starting/wake page, not a route
    await _drain()
    assert _activity_posts(requests_log) == []


async def test_idle_pending_workspace_is_routable_and_touches_activity(
    client, valid_cookie, requests_log, routing_config
):
    routing_config.update(state="idle_pending", agent_ready=True)
    resp = await client.get("/canvas/")
    assert resp.status_code == 200
    upstream = [r for r in requests_log if r.url.host == "10.0.0.5"]
    assert len(upstream) == 1  # proxied to the pod, no wake page
    await _drain()
    assert len(_activity_posts(requests_log)) == 1
