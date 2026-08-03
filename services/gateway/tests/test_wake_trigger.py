"""Wake trigger single-flight & throttle: at most one /start POST per
workspace in flight, and no more than one per workspace per 5s window.
Skipped wakes show the starting page exactly as if a trigger were already
in progress, and different workspaces are never throttled against each
other."""

import asyncio
import time

import main
from conftest import _ROUTING_ACL


def _start_posts(requests_log):
    return [
        r for r in requests_log
        if r.method == "POST" and r.url.path.startswith("/api/workspaces/") and r.url.path.endswith("/start")
    ]


async def _drain():
    # Let fire-and-forget wake triggers run to completion.
    await asyncio.sleep(0.05)


async def test_rapid_wakes_same_workspace_single_post(client, valid_cookie, routing_config, requests_log):
    routing_config.update(state="hibernated", agent_ready=False)
    for _ in range(2):
        resp = await client.get("/canvas/")
        assert resp.status_code == 200
        assert "Your workspace is starting" in resp.text
    await _drain()
    posts = _start_posts(requests_log)
    assert len(posts) == 1
    assert posts[0].url.path == "/api/workspaces/ws-alice/start"


async def test_wake_skipped_while_trigger_in_flight(client, valid_cookie, routing_config, requests_log, monkeypatch):
    routing_config.update(state="hibernated", agent_ready=False)
    gate = asyncio.Event()
    fired = []

    async def _slow_trigger(workspace_id, session_cookie=None):
        fired.append(workspace_id)
        await gate.wait()

    monkeypatch.setattr(main, "trigger_start", _slow_trigger)
    try:
        resp = await client.get("/canvas/")
        assert resp.status_code == 200
        assert "Your workspace is starting" in resp.text
        # In-flight marker is set synchronously, before the response is out.
        assert "ws-alice" in main._WAKE_TRIGGERS_IN_FLIGHT
        resp2 = await client.get("/canvas/")
        assert resp2.status_code == 200
        assert "Your workspace is starting" in resp2.text
        await asyncio.sleep(0.02)  # let the first trigger actually run
        assert fired == ["ws-alice"]  # second wake did not fire a new trigger
        assert _start_posts(requests_log) == []  # nothing hit the control plane
    finally:
        gate.set()  # never leave the trigger task blocked in the session loop
    await asyncio.sleep(0.02)
    assert "ws-alice" not in main._WAKE_TRIGGERS_IN_FLIGHT  # cleared on completion


async def test_wake_fires_again_after_window(client, valid_cookie, routing_config, requests_log):
    routing_config.update(state="hibernated", agent_ready=False)
    await client.get("/canvas/")
    await _drain()
    assert len(_start_posts(requests_log)) == 1
    assert "ws-alice" not in main._WAKE_TRIGGERS_IN_FLIGHT  # trigger completed
    # Backdate the last-trigger timestamp past the 5s window → next wake fires.
    main._LAST_WAKE_TRIGGER_AT["ws-alice"] = time.monotonic() - 60
    await client.get("/canvas/")
    await _drain()
    posts = _start_posts(requests_log)
    assert len(posts) == 2
    assert "ws-alice" not in main._WAKE_TRIGGERS_IN_FLIGHT


async def test_wake_not_throttled_across_workspaces(client, valid_cookie, routing_config, requests_log, monkeypatch):
    routing_config.update(state="hibernated", agent_ready=False)
    # Grant bob routing to his own workspace for this test only.
    monkeypatch.setattr("conftest._ROUTING_ACL", {**_ROUTING_ACL, "ws-bob": {"bob"}})
    await client.get("/canvas/")  # alice → ws-alice
    await _drain()
    client.cookies.set("session", "bob-cookie")
    await client.get("/canvas/")  # bob → ws-bob
    await _drain()
    posts = _start_posts(requests_log)
    assert len(posts) == 2
    assert {p.url.path.split("/")[3] for p in posts} == {"ws-alice", "ws-bob"}
