#!/bin/bash
# Start code-server in background
if command -v code-server > /dev/null 2>&1; then
    code-server --bind-addr 0.0.0.0:8080 --auth none /workspace &
fi

# Start workspace-agent in background  
if [ -f /usr/local/bin/workspace-agent ]; then
    /usr/local/bin/workspace-agent &
fi

# Start Agent Canvas (full stack, ingress on :8000)
export OH_CANVAS_SAFE_STATE_DIR=/workspace/.openhands
if command -v agent-canvas > /dev/null 2>&1; then
    agent-canvas --port 8000 &
fi
# Ensure Paseo config exists with proxy-aware settings
PASEO_HOME="${PASEO_HOME:-$HOME/.paseo}"
mkdir -p "$PASEO_HOME"
if [ ! -f "$PASEO_HOME/config.json" ]; then
    cat > "$PASEO_HOME/config.json" << "CONFEOF"
{
  "version": 1,
  "daemon": {
    "listen": "0.0.0.0:6767",
    "hostnames": ["${PASEO_HOSTNAME:-chat}.${BASE_DOMAIN:-example.com}", "*.${BASE_DOMAIN:-example.com}"],
    "trustedProxies": ["${K8S_POD_CIDR:-10.42.0.0/16}", "${K8S_SERVICE_CIDR:-10.43.0.0/16}"],
    "cors": {
      "allowedOrigins": [
        "http://${PASEO_HOSTNAME:-chat}.${BASE_DOMAIN:-example.com}:31060",
        "http://${BASE_DOMAIN:-example.com}:31060"
      ]
    },
    "relay": { "enabled": true }
  },
  "app": { "baseUrl": "http://${PASEO_HOSTNAME:-chat}.${BASE_DOMAIN:-example.com}:31060" },
  "features": { "webUi": { "enabled": true } }
}
CONFEOF
fi

# Run the original Paseo entrypoint
exec /usr/bin/tini -- /usr/local/bin/paseo-docker-entrypoint "$@"
