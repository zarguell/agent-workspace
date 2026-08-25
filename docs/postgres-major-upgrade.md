# PostgreSQL major-version upgrades (Docker Compose)

The local dev stack persists Postgres data in a **version-pinned named volume**
(`pgdata18`). PostgreSQL major versions cannot share a data directory, so a
major-version bump (e.g. `16-alpine` → `18-alpine`) needs an explicit step
before the new server will start cleanly.

Two things changed for PG18 that matter here:

1. **Major-version data-dir incompatibility (all majors).** A PostgreSQL
   server refuses to start on a data directory initialized by a *different*
   major version. A PG18 container pointed at a PG16 `PGDATA` aborts with
   `database files are incompatible with server` / `data directory was
   initialized by PostgreSQL version 16`. There is no in-place reuse — the
   directory must be re-initialized (fresh init) or migrated (`pg_upgrade`).
2. **Data checksums on by default (new in PG18).** `initdb` now enables data
   checksums unless told otherwise. When migrating with `pg_upgrade`, this
   causes a mismatch if the *source* PG16 cluster has checksums **disabled**
   (the pre-18 default): `pg_upgrade` aborts unless the new PG18 cluster is
   initialized with `--no-data-checksums` first. You can enable checksums
   offline afterwards with `pg_checksums --enable`.

The `compose.yaml` volume is version-pinned (`pgdata18`) so a fresh checkout
always creates a clean PG18 data directory. The previous `pgdata` volume (if
any) is left untouched — it is never written to again, so it is **not
corrupted**, merely orphaned.

## Option A — start fresh (recommended for local dev / PoC)

The stack is not deployed anywhere and the volume is local-only, so a fresh
init is the simplest and safest path. A new checkout already uses `pgdata18`,
so just bring the stack up:

```bash
docker compose down        # stop, keep volume
docker compose up -d       # PG18 inits a fresh pgdata18 directory
```

To discard all local data and guarantee a clean slate:

```bash
docker compose down -v      # removes the pgdata18 volume
docker compose up -d
```

Reclaim space from the retired pre-PG18 volume (only if you no longer need it):

```bash
docker volume rm agent-platform-local_pgdata
```

## Option B — preserve data with `pg_upgrade`

Use this only if you have real data in an old `pgdata` (PG16) volume that you
must keep. **Take a backup first** (`scripts/backup.sh`).

```bash
OLD=16 NEW=18
docker compose down                                  # stop, keep volumes

# Create + initialize a NEW PG18 data dir. Use --no-data-checksums so pg_upgrade
# does not abort on the checksums mismatch vs the PG16 default.
docker volume create "agent-platform-local_pgdata18"
docker run --rm --user postgres \
  -v "agent-platform-local_pgdata18:/var/lib/postgresql/data" \
  "postgres:${NEW}-alpine" initdb -D /var/lib/postgresql/data --no-data-checksums

# Run pg_upgrade: old PG16 dir -> new PG18 dir (link mode, -k).
docker run --rm --user postgres \
  -v "agent-platform-local_pgdata:/old" \
  -v "agent-platform-local_pgdata18:/var/lib/postgresql/data" \
  "postgres:${NEW}-alpine" bash -c \
  "pg_upgrade -b /usr/lib/postgresql/${OLD}/bin \
              -B /usr/lib/postgresql/${NEW}/bin \
              -d /old -D /var/lib/postgresql/data -k"

# (Optional) enable checksums offline now that the data is PG18:
docker run --rm --user postgres \
  -v "agent-platform-local_pgdata18:/var/lib/postgresql/data" \
  "postgres:${NEW}-alpine" pg_checksums --enable -D /var/lib/postgresql/data

# Bring the stack back up; it now uses the migrated pgdata18 volume.
docker compose up -d
```

After either option, restore from a dump if needed (`scripts/restore.sh`) and
restart the control plane so its idempotent migrations re-run:

```bash
docker compose restart control-plane
```
