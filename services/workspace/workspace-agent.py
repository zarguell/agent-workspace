#!/usr/bin/python3
"""Workspace agent: readiness, activity, dev-server registration."""
import json, os, socket, subprocess, sys, time, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
PORT = int(os.environ.get("AGENT_PORT", "9000"))

# Dev server registry
registered_ports = {}
ALLOWED_PORTS = {6767, 3000, 4173, 5000, 5173, 8000, 8081}

class AgentHandler(BaseHTTPRequestHandler):
    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok"})
        elif self.path == "/ready":
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
        if self.path.startswith("/expose/"):
            port_str = self.path.split("/expose/")[1].split("/")[0]
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

def main():
    global start_time
    start_time = time.time()
    server = HTTPServer(("0.0.0.0", PORT), AgentHandler)
    print(f"workspace-agent listening on :{PORT}", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    main()
