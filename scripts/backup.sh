#!/bin/bash
# Backup the agent-platform control-plane database.
#
# Dumps the Postgres database via the compose postgres service into
# backups/agentplatform-<timestamp>.sql (plain SQL) or
# backups/agentplatform-<timestamp>.sql.enc (when BACKUP_ENCRYPT_KEY is set).
# Keeps the most recent KEEP backups, prunes the rest.
#
# SECURITY WARNING: the dump contains password hashes and live session
# tokens. Treat it as sensitive credentials:
#   - the backups directory is created under umask 077 and every dump file
#     is chmod 600; do not relax that
#   - unless BACKUP_ENCRYPT_KEY is set the dump is stored UNENCRYPTED —
#     set it to encrypt dumps at rest (AES-256-CBC, PBKDF2)
#
# Usage: scripts/backup.sh
#   BACKUP_DIR  where dumps go            (default: ./backups)
#   KEEP        number of backups to keep (default: 10)
#   BACKUP_ENCRYPT_KEY  when set, the dump is piped through
#                       `openssl enc -aes-256-cbc -salt -pbkdf2` and written
#                       as a .sql.enc file (decrypt with scripts/restore.sh
#                       using the same key)
#
# For a Kubernetes deployment, run the equivalent pg_dump against the
# postgres pod instead:
#   kubectl exec deploy/... -- pg_dump -U agent -d agentplatform > backup.sql
set -euo pipefail

cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP="${KEEP:-10}"
STAMP="$(date +%Y%m%d-%H%M%S)"
if [ -n "${BACKUP_ENCRYPT_KEY:-}" ]; then
    EXT="sql.enc"
else
    EXT="sql"
fi
DEST="$BACKUP_DIR/agentplatform-$STAMP.$EXT"

if ! docker compose ps --status running postgres >/dev/null 2>&1; then
    echo "error: compose postgres service is not running (start with: docker compose up -d postgres)" >&2
    exit 1
fi
if [ "$EXT" = "sql.enc" ] && ! command -v openssl >/dev/null 2>&1; then
    echo "error: BACKUP_ENCRYPT_KEY is set but openssl is not installed" >&2
    exit 1
fi

umask 077
mkdir -p "$BACKUP_DIR"
if [ "$EXT" = "sql.enc" ]; then
    echo "Dumping (encrypted) agentplatform -> $DEST"
    docker compose exec -T postgres pg_dump -U agent -d agentplatform \
        | openssl enc -aes-256-cbc -salt -pbkdf2 -pass env:BACKUP_ENCRYPT_KEY > "$DEST"
else
    echo "warning: BACKUP_ENCRYPT_KEY is not set — writing UNENCRYPTED dump; it contains password hashes and session tokens" >&2
    echo "Dumping agentplatform -> $DEST"
    docker compose exec -T postgres pg_dump -U agent -d agentplatform > "$DEST"
fi
chmod 600 "$DEST"
echo "Backup complete: $(wc -c < "$DEST") bytes"

# Prune old backups, keeping the KEEP newest.
ls -1t "$BACKUP_DIR"/agentplatform-*.sql* 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    echo "Pruning $old"
    rm -f "$old"
done
