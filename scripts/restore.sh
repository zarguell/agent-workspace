#!/bin/bash
# Restore the agent-platform control-plane database from a backup dump.
#
# Usage: scripts/restore.sh [dump-file]
#   Dump file defaults to the newest agentplatform-*.sql or *.sql.enc in
#   ./backups. Encrypted (.sql.enc) dumps are decrypted with
#   BACKUP_ENCRYPT_KEY (AES-256-CBC, PBKDF2) before being loaded.
#
# The restore wipes the public schema and re-imports the dump, so the target
# database ends up exactly as it was at backup time. The control-plane and
# gateway services do NOT need to be stopped, but the restore must not run
# concurrently with heavy writes. After restoring, existing sessions stay
# valid (the sessions table is part of the dump).
#
# SECURITY: the dump contains password hashes and live session tokens. Keep
# backups (and BACKUP_ENCRYPT_KEY) in a restricted location.
#
# For a Kubernetes deployment, load the dump into the postgres pod instead:
#   kubectl exec -i deploy/... -- psql -U agent -d agentplatform < backup.sql
set -euo pipefail

cd "$(dirname "$0")/.."

DUMP="${1:-}"
if [ -z "$DUMP" ]; then
    DUMP="$(ls -1t backups/agentplatform-*.sql* 2>/dev/null | head -1 || true)"
fi
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
    echo "error: no backup dump found (pass one: scripts/restore.sh backups/agentplatform-<stamp>.sql[.enc])" >&2
    exit 1
fi

if ! docker compose ps --status running postgres >/dev/null 2>&1; then
    echo "error: compose postgres service is not running (start with: docker compose up -d postgres)" >&2
    exit 1
fi

case "$DUMP" in
    *.sql.enc)
        if [ -z "${BACKUP_ENCRYPT_KEY:-}" ]; then
            echo "error: $DUMP is encrypted but BACKUP_ENCRYPT_KEY is not set — cannot decrypt" >&2
            exit 1
        fi
        if ! command -v openssl >/dev/null 2>&1; then
            echo "error: openssl is required to decrypt $DUMP but is not installed" >&2
            exit 1
        fi
        ENCRYPTED=1
        ;;
    *)
        ENCRYPTED=0
        ;;
esac

echo "Restoring $DUMP into agentplatform — this DROPS the public schema."
read -r -p "Type 'yes' to continue: " CONFIRM
if [ "$CONFIRM" != "yes" ]; then
    echo "aborted: restore cancelled" >&2
    exit 1
fi

docker compose exec -T postgres psql -U agent -d agentplatform \
    -v ON_ERROR_STOP=1 \
    -c "DROP SCHEMA public CASCADE" \
    -c "CREATE SCHEMA public"
if [ "$ENCRYPTED" -eq 1 ]; then
    echo "Loading (decrypted) $DUMP"
    openssl enc -d -aes-256-cbc -salt -pbkdf2 -pass env:BACKUP_ENCRYPT_KEY -in "$DUMP" \
        | docker compose exec -T postgres psql -U agent -d agentplatform -v ON_ERROR_STOP=1
else
    echo "Loading $DUMP"
    docker compose exec -T postgres psql -U agent -d agentplatform -v ON_ERROR_STOP=1 < "$DUMP"
fi
echo "Restore complete. Restart the control plane to re-run idempotent migrations:"
echo "  docker compose restart control-plane"
