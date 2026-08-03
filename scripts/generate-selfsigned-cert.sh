#!/bin/bash
# Generate a self-signed CA + leaf certificate for local HTTPS and install it
# as a Kubernetes TLS secret consumed by the Traefik ingress.
#
# Usage:
#   ./scripts/generate-selfsigned-cert.sh [namespace] [domain]
#   ./scripts/generate-selfsigned-cert.sh agent-platform agents.local.test
#
# After running:
#   1. Set tls.enabled: true in values.local.yaml and re-deploy the chart.
#   2. Trust certs/agent-platform-ca.crt on client machines (or accept the
#      browser warning) so HTTPS works without certificate errors.
#
# The private key stays local under certs/ (gitignored) — never commit it.
set -euo pipefail

NAMESPACE="${1:-agent-platform}"
DOMAIN="${2:-agents.local.test}"
CERT_DIR="${CERT_DIR:-$(pwd)/certs}"
SECRET_NAME="agent-platform-tls"
DAYS=3650

mkdir -p "$CERT_DIR"

# CA key + certificate
openssl req -x509 -newkey rsa:2048 -nodes -days "$DAYS" \
  -keyout "$CERT_DIR/ca.key" -out "$CERT_DIR/ca.crt" \
  -subj "/CN=agent-platform-local-ca" \
  -addext "basicConstraints=critical,CA:TRUE"

# Leaf key + CSR
openssl req -newkey rsa:2048 -nodes \
  -keyout "$CERT_DIR/tls.key" -out "$CERT_DIR/tls.csr" \
  -subj "/CN=${DOMAIN}"

# SAN config: the domain, its wildcard, plus localhost for convenience
cat > "$CERT_DIR/openssl.cnf" <<EOF
[ext]
subjectAltName = @alt_names

[alt_names]
DNS.1 = ${DOMAIN}
DNS.2 = *.${DOMAIN}
DNS.3 = localhost
IP.1 = 127.0.0.1
EOF

# Sign the leaf cert with the CA
openssl x509 -req -days "$DAYS" \
  -in "$CERT_DIR/tls.csr" \
  -CA "$CERT_DIR/ca.crt" -CAkey "$CERT_DIR/ca.key" -CAcreateserial \
  -out "$CERT_DIR/tls.crt" \
  -extfile "$CERT_DIR/openssl.cnf" -extensions ext

# Install as K8s TLS secret (namespace must exist)
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"
kubectl -n "$NAMESPACE" create secret tls "$SECRET_NAME" \
  --cert="$CERT_DIR/tls.crt" --key="$CERT_DIR/tls.key" \
  --dry-run=client -o yaml | kubectl apply -f -

echo ""
echo "TLS secret '$SECRET_NAME' installed in namespace '$NAMESPACE'."
echo "CA certificate (trust this on clients to avoid warnings):"
echo "  $CERT_DIR/ca.crt"
echo ""
echo "Next steps:"
echo "  1. Set tls.enabled: true in values.local.yaml"
echo "  2. helm upgrade --install agent-platform charts/agent-platform \\"
echo "       -f charts/agent-platform/values.local.yaml --namespace agent-platform"
echo "  3. Access via https://${DOMAIN}/ (forward the websecure NodePort, default 30118)"
