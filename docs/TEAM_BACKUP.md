# Team server backup: snapshots and continuous replication

The team server keeps everything in SQLite files under its data directory (`--root`, `TAM_TEAM_DIR`):

| File | Holds |
|---|---|
| `identity.db` | users, departments, membership, token and password hashes, sessions, audit log, encrypted provider settings |
| `learning.db` | onboarding curricula, progress, quiz attempts, notes waiting for personal memory |
| `workspaces/shared/memory.db` | company-wide memory |
| `workspaces/team_<sha256>/memory.db` | one per department |
| `workspaces/personal_<sha256>/memory.db` | one per person |

There are two ways to back them up. They work together. Both cover the SQLite storage backend only: a server whose data lives in PostgreSQL is backed up with PostgreSQL tooling (`pg_dump`, WAL archiving, the provider's managed backups), and on such a server `tam-team replication` and the setup wizard's backup step say so and change nothing.

On PostgreSQL, `tam-team backup --out DIR` runs `pg_dump` of the TAM schemas online (manifest format 2 with the dump's SHA-256 and every table's row count) and `tam-team restore --from DIR --dsn-env VAR` restores it into an empty database and re-creates the workspace roles; for continuous protection use the provider's point-in-time recovery or WAL archiving. See [TEAM_POSTGRES.md](TEAM_POSTGRES.md#backup-and-restore). After a migration to PostgreSQL the SQLite files move to `archive/sqlite-<timestamp>/` and Litestream no longer replicates them.

| | `tam-team backup` / `restore` | Continuous replication (Litestream) |
|---|---|---|
| Server | must be stopped | keeps running |
| Result | a verified snapshot directory with SHA-256 manifest | a bucket or directory that receives every committed transaction within about `TAM_TEAM_REPLICA_SYNC_INTERVAL` (1 s by default) |
| Restore to | the moment of the snapshot | any moment inside the retention window (`--timestamp`), or the latest transaction |
| Extra software | none | [Litestream](https://litestream.io) 0.5.4 or newer (tested with 0.5.17), as a binary or the `litestream/litestream` image |

Both restores create a **new** data directory and publish it with one rename after every database passed SQLite `quick_check` and a foreign-key check. Neither overwrites a live server.

Neither includes `master.key`, the key that decrypts provider API keys saved in the dashboard. Keep it (or `TAM_TEAM_MASTER_KEY`) in your secret store; a replica without the key still restores users, memory and history, but the saved provider keys must be entered again.

## Continuous replication

Litestream runs next to the server as its own process. It reads the SQLite write-ahead log and uploads each transaction to the replica. The generated configuration watches two directories:

- the data directory itself for `*.db` (`identity.db`, `learning.db`), and
- `workspaces/` recursively for `memory.db`.

Litestream's directory watcher starts replicating a workspace as soon as its `memory.db` appears (a new department, a person's first save) and stops when it is deleted, so the configuration never has to be regenerated when people or departments change.

It is off by default. Everything is configured through environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `TAM_TEAM_REPLICA_URL` | (unset: off) | `s3://bucket/path` for S3 and S3-compatible stores, or `file:///absolute/dir` for a mounted disk or NAS share. No credentials, port or query in the URL. |
| `TAM_TEAM_REPLICA_ENDPOINT` | AWS | `https://host[:port]` of a non-AWS store: MinIO, RustFS, Backblaze B2, Cloudflare R2, Wasabi. |
| `TAM_TEAM_REPLICA_REGION` | `us-east-1` | Bucket region. |
| `TAM_TEAM_REPLICA_FORCE_PATH_STYLE` | `true` when an endpoint is set | Path-style bucket addressing; MinIO and RustFS need it. |
| `TAM_TEAM_REPLICA_SYNC_INTERVAL` | `1s` | How often new transactions are uploaded. |
| `TAM_TEAM_REPLICA_SNAPSHOT_INTERVAL` | `24h` | How often Litestream writes a full snapshot. |
| `TAM_TEAM_REPLICA_RETENTION` | `168h` | How long snapshots and transaction files are kept: the point-in-time window. Must be at least the snapshot interval. |
| `TAM_TEAM_REPLICA_METRICS_ADDR` | (none) | `host:port` for Litestream's Prometheus metrics, e.g. `127.0.0.1:9090`. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (`AWS_SESSION_TOKEN`) | | Bucket credentials, read by Litestream and by `tam-team replication`. Never written to generated files. |
| `TAM_TEAM_LITESTREAM_BIN` | `litestream` | The Litestream executable `replication restore` runs. |

Durations use Go syntax: `90s`, `30m`, `168h`.

Give the credentials only the bucket (or prefix) they need: list, read, write and delete objects. Do not share one replica path between two servers; Litestream cannot restore a path two writers used.

### Without Docker

```sh
export TAM_TEAM_REPLICA_URL=s3://acme-tam-backup/prod
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...

tam-team --root /srv/tam-team replication init-bucket          # optional: creates the bucket
tam-team --root /srv/tam-team replication config --out /etc/tam/litestream.yml
tam-team --root /srv/tam-team replication prepare              # server stopped; see "WAL mode" below
litestream replicate -config /etc/tam/litestream.yml          # run as a service, as the server's user
tam-team --root /srv/tam-team replication status
```

`config` writes the file with mode 0600 and creates `workspaces/` (0700) so Litestream can watch it before the first workspace exists. Without `--out` it prints the configuration.

Run Litestream as the same OS user as the server: both write the `-wal` and `-shm` files and Litestream keeps its metadata in `.<name>-litestream` directories next to each database.

`tam setup --mode company` asks for continuous backup as its last step (or `--backup s3|file --backup-url ... [--backup-endpoint ...] [--backup-region ...] [--backup-retention ...]` with `--non-interactive`). It writes `deploy/litestream.yml` and `deploy/replication.env` (both 0600) into the data directory and, for a Linux service, `deploy/tam-team-litestream.service`, which reads the credentials from `deploy/replica-credentials.env`. The wizard never stores credentials; create that file yourself with mode 0600.

### Docker Compose

`docker-compose.team.yml` has a `litestream` sidecar in the `litestream` profile. It mounts the server's data volume and `docker/litestream.team.yml`, which Litestream fills in from the `TAM_TEAM_REPLICA_*` variables at start. It runs as UID 1000 like the server.

```sh
# .env next to docker-compose.team.yml (mode 0600)
TAM_TEAM_REPLICA_URL=s3://acme-tam-backup/prod
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...

docker compose -f docker-compose.team.yml --profile litestream up -d
docker compose -f docker-compose.team.yml logs litestream
docker compose -f docker-compose.team.yml exec -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY team-memory \
    python /app/src/team_memory/cli.py replication status
```

The sidecar supports S3 replicas; for a directory replica run Litestream on the host.

To try it locally, the `s3-local` profile adds an S3-compatible store ([RustFS](https://rustfs.com), same API as MinIO, published on `127.0.0.1:${TAM_S3_LOCAL_PORT:-9000}`) and a one-shot container that creates the bucket. A bucket on the same machine is not a backup; use it for testing only.

```sh
# .env
TAM_TEAM_REPLICA_URL=s3://tam-local/team
TAM_TEAM_REPLICA_ENDPOINT=http://replica-s3:9000
TAM_TEAM_REPLICA_FORCE_PATH_STYLE=true
AWS_ACCESS_KEY_ID=tamlocal
AWS_SECRET_ACCESS_KEY=choose-a-long-random-secret

docker compose -f docker-compose.team.yml --profile litestream --profile s3-local up -d
```

MinIO no longer publishes community images on Docker Hub; set `TAM_S3_LOCAL_IMAGE` only to an image that reads `RUSTFS_ACCESS_KEY`/`RUSTFS_SECRET_KEY`, or run your own MinIO and point `TAM_TEAM_REPLICA_ENDPOINT` at it.

### Checking it

```sh
tam-team --root /srv/tam-team replication status        # add --json for monitoring
```

For every database it shows whether it is `replicated`, `pending` (no replica yet: Litestream is not running or cannot reach the bucket) or `orphaned` (only in the replica; see purging below), its journal mode, the number of replica files and the last upload time. The exit code is 1 when a database is pending or not in WAL mode. An idle database uploads nothing, so an old "last upload" alone is not a fault.

Litestream logs JSON lines; with `TAM_TEAM_REPLICA_METRICS_ADDR` it also serves Prometheus metrics at `/metrics` for alerting on failed syncs.

### Restore

```sh
export TAM_TEAM_REPLICA_URL=s3://acme-tam-backup/prod AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=...
tam-team replication restore --to /srv/tam-team-restored                                  # latest
tam-team replication restore --to /srv/tam-team-restored --timestamp 2026-09-25T14:30:00Z # point in time
cp /srv/tam-team/master.key /srv/tam-team-restored/        # or set TAM_TEAM_MASTER_KEY
tam-team --root /srv/tam-team-restored serve
```

`restore` needs no running server and works on any machine that can reach the replica. It lists the databases in the replica, restores each with `litestream restore` into a staging directory next to `--to`, checks them, writes `restored-from.json` and renames the staging directory to `--to`. `--to` must not exist. A workspace created after `--timestamp` did not exist then and is reported as skipped. `--timestamp` is ISO 8601 with a time zone and must lie inside the retention window.

Each database is restored to its own last transaction at or before `--timestamp`. SQLite has no transaction spanning files, so two databases are consistent to within one sync interval, the same as the running server sees them.

On a host without Litestream, the repository's `scripts/litestream-docker` runs it from the official image:

```sh
TAM_TEAM_LITESTREAM_BIN=$PWD/scripts/litestream-docker TAM_LITESTREAM_DOCKER_MOUNT=/srv \
    tam-team replication restore --to /srv/tam-team-restored
```

`TAM_LITESTREAM_DOCKER_MOUNT` is mounted at the same path in the container and must contain the parent of `--to`.

To put a restored directory into the Compose volume, stop the stack, copy it into a new volume and point `TAM_TEAM_DATA_VOLUME` at that volume:

```sh
docker volume create tam-team-memory-data-restored
docker run --rm -v /srv/tam-team-restored:/from:ro -v tam-team-memory-data-restored:/to alpine \
    sh -c 'cp -a /from/. /to/ && chown -R 1000:1000 /to'
```

The two kinds of backup compose: `tam-team --root /srv/tam-team-restored backup --out ...` takes a snapshot of a restored directory, and a server restored with `tam-team restore --from ...` can replicate to a **new** replica path.

## WAL mode and checkpoints

Litestream needs SQLite's WAL journal mode and must be the process that checkpoints the WAL:

- Workspace databases are opened in WAL mode by the memory store. `identity.db` and `learning.db` use SQLite's default rollback journal until something switches them. Litestream switches every database it watches when it starts; `tam-team replication prepare` does it ahead of time with the server stopped (it takes the same locks as `backup`). WAL mode is stored in the database file, so the server keeps using it afterwards.
- The team server never runs `PRAGMA wal_checkpoint(TRUNCATE|RESTART|FULL)`; a test guards this. It relies on SQLite's automatic passive checkpoint, which cannot restart the WAL while Litestream holds its read transaction. Litestream 0.5 detects checkpoints it did not make; its documentation still suggests `PRAGMA wal_autocheckpoint=0` for very high rates of small transactions, far above what a team memory server writes.
- Every server connection waits for locks (`busy_timeout` 5–10 s), so Litestream's own short checkpoint locks do not surface as errors.

## Deleting data: purge, departments and retention

Replication copies deletions too, but history stays in the replica for a while:

- **A row deleted in a database that still exists** (a record deleted from team memory, a revoked token, a user's onboarding notes removed by `user-purge`) disappears from new snapshots. Older snapshots and transaction files are removed by Litestream once they are older than `TAM_TEAM_REPLICA_RETENTION`, so the deleted data is gone from the replica at most about retention + snapshot interval after the deletion (8 days with the defaults). Point-in-time restore inside that window can bring it back; that is what the window is for.
- **A deleted database file** (a personal area removed by `tam-team user-purge`) stops being replicated, but **Litestream never deletes its replica**: retention only runs for databases it still watches. Remove it explicitly after the purge:

  ```sh
  tam-team --root /srv/tam-team user-purge petya --confirm petya
  tam-team --root /srv/tam-team replication drop --user petya --confirm petya
  ```

  `user-purge` reminds you of this when `TAM_TEAM_REPLICA_URL` is set. `drop` refuses while the workspace still exists locally (Litestream would upload it again), deletes every object under `<replica>/workspaces/personal_<sha256>/memory.db/`, lists the prefix again to make sure Litestream did not recreate it, and records `replica_dropped` in the audit log. `--team ID` does the same for a department workspace and `--workspace KEY` for any key `replication status` lists as orphaned.
- **Departments** with stored memory cannot be deleted by the server today, so their replicas only change through retention.
- **Snapshots made with `tam-team backup`** and bucket versioning or object-lock policies are outside Litestream's reach. If the bucket keeps object versions, deleted objects remain as old versions: set a lifecycle rule that expires noncurrent versions within your deletion deadline, or use a bucket without versioning for the replica.

For a strict erasure deadline shorter than the retention window, lower `TAM_TEAM_REPLICA_RETENTION` (it bounds how far back point-in-time restore can go) and keep the separate `tam-team backup` snapshots under the same policy.
