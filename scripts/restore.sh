#!/bin/bash
# Restore the agent-platform control-plane database from a backup dump.
#
# Usage: scripts/restore.sh [dump-file]
#   Dump file defaults to the newest agentplatform-*.sql in ./backups.
#
# The restore wipes the public schema and re-imports the dump, so the target
# database ends up exactly as it was at backup time. The control-plane and
# gateway services do NOT need to be stopped, but the restore must not run
# concurrently with heavy writes. After restoring, existing sessions stay
# valid (the sessions table is part of the dump).
#
# For a Kubernetes deployment, load the dump into the postgres pod instead:
#   kubectl exec -i deploy/... -- psql -U agent -d agentplatform < backup.sql
set -euo pipefail

cd "$(dirname "$0")/.."

DUMP="${1:-}"
if [ -z "$DUMP" ]; then
    DUMP="$(ls -1t backups/agentplatform-*.sql 2>/dev/null | head -1 || true)"
fi
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
    echo "error: no backup dump found (pass one: scripts/restore.sh backups/agentplatform-<stamp>.sql)" >&2
    exit 1
fi

if ! docker compose ps --status running postgres >/dev/null 2>&1; then
    echo "error: compose postgres service is not running (start with: docker compose up -d postgres)" >&2
    exit 1
fi

echo "Restoring $DUMP into agentplatform (wiping public schema first)"
docker compose exec -T postgres psql -U agent -d agentplatform \
    -v ON_ERROR_STOP=1 \
    -c "DROP SCHEMA public CASCADE" \
    -c "CREATE SCHEMA public"
docker compose exec -T postgres psql -U agent -d agentplatform -v ON_ERROR_STOP=1 < "$DUMP"
echo "Restore complete. Restart the control plane to re-run idempotent migrations:"
echo "  docker compose restart control-plane"
