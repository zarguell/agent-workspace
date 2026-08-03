#!/bin/bash
# Backup the agent-platform control-plane database.
#
# Dumps the Postgres database via the compose postgres service into
# backups/agentplatform-<timestamp>.sql (plain SQL). Keeps the most recent
# KEEP backups, prunes the rest.
#
# Usage: scripts/backup.sh
#   BACKUP_DIR  where dumps go            (default: ./backups)
#   KEEP        number of backups to keep (default: 10)
#
# For a Kubernetes deployment, run the equivalent pg_dump against the
# postgres pod instead:
#   kubectl exec deploy/... -- pg_dump -U agent -d agentplatform > backup.sql
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP="${KEEP:-10}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_DIR/agentplatform-$STAMP.sql"

if ! docker compose ps --status running postgres >/dev/null 2>&1; then
    echo "error: compose postgres service is not running (start with: docker compose up -d postgres)" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
echo "Dumping agentplatform -> $DEST"
docker compose exec -T postgres pg_dump -U agent -d agentplatform > "$DEST"
echo "Backup complete: $(wc -c < "$DEST") bytes"

# Prune old backups, keeping the KEEP newest.
ls -1t "$BACKUP_DIR"/agentplatform-*.sql 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    echo "Pruning $old"
    rm -f "$old"
done
