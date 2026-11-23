# PostgreSQL major-version upgrades (Docker Compose)

The local dev stack persists Postgres data in a **version-pinned named volume**
(`pgdata19`). PostgreSQL major versions cannot share a data directory, so a
major-version bump (`18-alpine` → `19-alpine`, or the earlier `16-alpine` →
`18-alpine`) needs an explicit step before the new server will start cleanly.

Two things matter here:

1. **Major-version data-dir incompatibility (all majors).** A PostgreSQL
   server refuses to start on a data directory initialized by a *different*
   major version. A PG19 container pointed at a PG18 `PGDATA` exits with
   `database files are incompatible with server` / `data directory was
   initialized by PostgreSQL version 18`. There is no in-place reuse — the
   directory must be re-initialized (fresh init) or migrated.
2. **Data checksums (on by default since PG18).** `initdb` has enabled data
   checksums by default since PG18. The 16 → 18 step therefore needed care
   (`pg_upgrade` aborts on a checksum mismatch unless the new cluster is
   initialized with `--no-data-checksums`, after which `pg_checksums --enable`
   can turn them on offline). For 18 → 19 the setting is already on in both
   versions, so there is no checksum caveat on that step.

The `compose.yaml` volume is version-pinned (`pgdata19`) so a fresh checkout
always creates a clean PG19 data directory. The previous volumes (`pgdata18`,
and the pre-PG18 `pgdata`) are left untouched — never written to again, so they
are **not corrupted**, merely orphaned.

## Option A — start fresh (recommended for local dev / PoC)

The stack is not deployed anywhere and the volume is local-only, so a fresh
init is the simplest and safest path. A new checkout already uses `pgdata19`,
so just bring the stack up:

```bash
docker compose down        # stop, keep volumes
docker compose up -d       # PG19 inits a fresh pgdata19 directory
```

To discard all local data and guarantee a clean slate:

```bash
docker compose down -v      # removes the pgdata19 volume
docker compose up -d
```

Reclaim space from retired volumes (only if you no longer need them):

```bash
docker volume rm agent-platform-local_pgdata18 agent-platform-local_pgdata
```

## Option B — preserve data with dump / restore

Use this if you have real data in the old `pgdata18` (PG18) volume that you
must keep. **Take a backup first** (`scripts/backup.sh`). A `pg_dumpall` from
PG18 restores cleanly into PG19.

```bash
docker compose down                                  # stop, keep volumes

# 1. Dump the old PG18 cluster (temporary PG18 server on the old volume).
#    pg_dumpall is used so roles/globals come along too.
docker run -d --name pg18-old \
  -e PGDATA=/var/lib/postgresql/data \
  -e POSTGRES_USER=agent -e POSTGRES_DB=agentplatform \
  -v "agent-platform-local_pgdata18:/var/lib/postgresql/data" \
  "postgres:18-alpine"
docker exec -e PGPASSWORD=agent pg18-old pg_dumpall -U agent > pg18-dump.sql
docker rm -f pg18-old

# 2. Bring the stack up: PG19 initdbs a fresh pgdata19 volume.
docker compose up -d

# 3. Restore into the new cluster, then let the control plane re-run its
#    idempotent migrations.
#    "role \"agent\" already exists" / "database \"agentplatform\" already
#    exists" errors are expected and benign (the new image already created
#    them); everything else should apply cleanly.
docker compose exec -T postgres psql -U agent -d postgres < pg18-dump.sql
docker compose restart control-plane
```

## Option C — `pg_upgrade` (advanced, in-place)

`pg_upgrade` rewrites the data dir in place, so it is faster than a dump for
large clusters, but it needs **both** majors' binaries. The official
`postgres:*-alpine` images are single-major source builds installed under
`/usr/local` — there is **no** `/usr/lib/postgresql/<ver>/bin` path, and the
new image does not ship the old major's binaries. Copy them out of the old
image first:

```bash
OLD=18 NEW=19
docker compose down                                  # stop, keep volumes

# Old binaries from the PG18 image (single-major /usr/local source build).
docker create --name pg18-bin "postgres:${OLD}-alpine"
docker cp pg18-bin:/usr/local /tmp/pg18-local          # /tmp/pg18-local/bin/pg_upgrade etc.
docker rm pg18-bin

# New, empty PG19 data dir. Note:
#  - the volume root is owned by root, so chown it before initdb;
#  - initdb's superuser is "postgres" by default, but pg_upgrade connects
#    with the OLD cluster's superuser ("agent" here), so pass -U agent;
#  - checksums are on by default in both majors, so no --no-data-checksums
#    flag is needed for 18 -> 19.
docker volume create "agent-platform-local_pgdata19"
docker run --rm \
  -v "agent-platform-local_pgdata19:/var/lib/postgresql/data" \
  "postgres:${NEW}-alpine" chown -R postgres:postgres /var/lib/postgresql/data
docker run --rm --user postgres \
  -v "agent-platform-local_pgdata19:/var/lib/postgresql/data" \
  "postgres:${NEW}-alpine" initdb -U agent -D /var/lib/postgresql/data

# pg_upgrade: old PG18 dir -> new PG19 dir (copy mode, no -k).
#  - run it in a writable cwd with HOME set (pg_upgrade writes its logs there);
#  - PGUSER/`-U agent` must match the old cluster's superuser;
#  - copy mode (default) is used because the two Docker volumes are separate
#    filesystems — link mode (-k) aborts with "Cross-device link".
docker run --rm --user postgres -e HOME=/tmp -e PGUSER=agent \
  -v "agent-platform-local_pgdata18:/old" \
  -v "agent-platform-local_pgdata19:/var/lib/postgresql/data" \
  -v "/tmp/pg18-local:/tmp/pg18-local:ro" \
  "postgres:${NEW}-alpine" bash -c \
  "cd /tmp && pg_upgrade -U agent \
              -b /tmp/pg18-local/bin \
              -B /usr/local/bin \
              -d /old -D /var/lib/postgresql/data"

# When pg_upgrade reports success, refresh statistics in the new cluster and
# (optionally) drop the old data files.
docker run --rm --user postgres -e HOME=/tmp \
  -v "agent-platform-local_pgdata19:/var/lib/postgresql/data" \
  "postgres:${NEW}-alpine" bash -c \
  "cd /tmp && /usr/local/bin/vacuumdb -U agent --all --analyze-in-stages"

# Bring the stack back up; it now uses the migrated pgdata19 volume.
docker compose up -d
```

Caveats: the old `bin/` tree must be runnable inside the new image (both are
musl/alpine builds today, so this works; if the base Alpine versions diverge it
can fail on missing shared libraries — use Option B instead). Link mode
(`-k`) only works when both data dirs share a filesystem, e.g. if you mount one
volume and keep the old cluster in a subdirectory of it. `pg_upgrade` leaves
`delete_old_cluster.sh` + `pg_upgrade_output.d/` in the cwd; run the script from
that same container if you want to remove the old files.

After either preserving option, restart the control plane so its idempotent
migrations re-run:

```bash
docker compose restart control-plane
```

## Note: the Helm chart is still on PG16

`charts/agent-platform/templates/postgres-statefulset.yaml` still pins
`postgres:16-alpine` and mounts `/var/lib/postgresql/data` without a `PGDATA`
override. Bumping that chart is a separate change: PG18+ images default
`PGDATA` to a major-versioned subdirectory (`/var/lib/postgresql/<major>/docker`)
and the entrypoint exits 1 when the mounted volume is not the data directory, so
the StatefulSet needs the same version-pinned-volume + `PGDATA` treatment this
compose stack already has.
