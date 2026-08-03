#!/usr/bin/python3
"""Workspace agent: readiness, activity, dev-server registration, usage reporting."""
import hmac, json, os, socket, subprocess, sys, time, threading, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
PORT = int(os.environ.get("AGENT_PORT", "9000"))
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://control-plane:80").rstrip("/")
WORKSPACE_AGENT_TOKEN = os.environ.get("WORKSPACE_AGENT_TOKEN", "")
WORKSPACE_ID = os.environ.get("WORKSPACE_ID", "")
REPORT_INTERVAL = int(os.environ.get("REPORT_INTERVAL", "300"))

# Dev server registry
registered_ports = {}
ALLOWED_PORTS = {6767, 3000, 4173, 5000, 5173, 8000, 8081}


class AgentHandler(BaseHTTPRequestHandler):
    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _require_token(self):
        """403 unless the caller presents X-Pod-Token (constant-time compare).

        /health is exempt so the reconciler readiness probe stays open;
        every other endpoint requires the per-workspace agent token. A
        missing expected token (env unset) rejects all callers rather than
        falling open.
        """
        supplied = self.headers.get("X-Pod-Token", "")
        if not WORKSPACE_AGENT_TOKEN or not hmac.compare_digest(supplied, WORKSPACE_AGENT_TOKEN):
            self._respond(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
            return
        if not self._require_token():
            return
        if self.path == "/ready":
            # Check Paseo and code-server
            services = {}
            for name, port, path in [
                ("paseo", 6767, "/api/health"),
                ("code-server", 8080, "/"),
                ("canvas", 8000, "/health"),
            ]:
                try:
                    s = socket.create_connection(("127.0.0.1", port), timeout=2)
                    s.close()
                    services[name] = "ready"
                except Exception:
                    services[name] = "unreachable"
            all_ready = all(v == "ready" for v in services.values())
            self._respond(200 if all_ready else 503, {
                "status": "ready" if all_ready else "starting",
                "services": services,
            })
        elif self.path == "/password":
            password = os.environ.get("PASEO_PASSWORD", "")
            self._respond(200, {"password": password})
        elif self.path == "/status":
            self._respond(200, {
                "workspace": WORKSPACE,
                "registered_ports": {
                    str(k): v for k, v in registered_ports.items()
                },
                "uptime": time.time() - start_time,
            })
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if not self._require_token():
            return
        if self.path.startswith("/expose/"):
            try:
                port = int(port_str)
            except ValueError:
                self._respond(400, {"error": "invalid port"})
                return
            if port not in ALLOWED_PORTS:
                self._respond(403, {"error": f"port {port} not in allowlist"})
                return
            # Verify it's listening
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=2)
                s.close()
            except Exception:
                self._respond(409, {"error": f"port {port} not listening"})
                return
            registered_ports[port] = {
                "status": "registered",
                "timestamp": time.time(),
            }
            self._respond(200, {"port": port, "status": "registered"})
        elif self.path == "/activity":
            # Heartbeat endpoint
            self._respond(200, {"ok": True})
        else:
            self._respond(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass  # quiet


def report_usage_loop():
    """Periodically report compute/storage usage to the control plane.

    Best-effort: transient failures (control plane down, quota 429) are
    swallowed and retried on the next interval. Token usage is NOT
    observable here — that requires agent-server/Canvas integration and is
    a separate emitter.
    """
    while True:
        try:
            events = [
                {
                    "category": "compute",
                    "metric": "agent_uptime_seconds",
                    "amount": int(time.time() - start_time),
                    "unit": "s",
                    "workspace_id": WORKSPACE_ID,
                },
            ]
            try:
                du = subprocess.run(["du", "-sk", WORKSPACE], capture_output=True, text=True, timeout=15)
                if du.returncode == 0:
                    events.append({
                        "category": "storage",
                        "metric": "workspace_kb",
                        "amount": int(du.stdout.split()[0]),
                        "unit": "kB",
                        "workspace_id": WORKSPACE_ID,
                    })
            except Exception:
                pass

            if not WORKSPACE_AGENT_TOKEN:
                time.sleep(REPORT_INTERVAL)
                continue

            req = urllib.request.Request(
                f"{CONTROL_PLANE_URL}/api/internal/usage",
                data=json.dumps({"events": events}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Workspace-Token": WORKSPACE_AGENT_TOKEN,
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass  # retry next interval
        time.sleep(REPORT_INTERVAL)


def main():
    global start_time
    start_time = time.time()
    threading.Thread(target=report_usage_loop, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), AgentHandler)
    print(f"workspace-agent listening on :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
