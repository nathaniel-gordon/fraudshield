# Deployment

**Nothing is deployed anywhere today.** There is no service, no live URL and no
managed environment behind this repository. This document describes how to run the
container on a host you own, and it is written for the target that actually fits the
architecture — not for one that flatters it.

## Target: a single Docker host

FraudShield is one process reading and writing one SQLite file. That shape decides
the deployment:

- there is no network listener, so nothing needs a load balancer, an ingress or a
  service mesh;
- the state is one file on one filesystem, and SQLite's locking is the concurrency
  story — a second host adds corruption risk, not capacity;
- runs are batch-shaped (ingest, query, seal, export) with an interactive console on
  the side.

So the target is: **one Linux VM (or any machine with Docker) running the image
against a named volume**, with scheduled runs driven by cron or a systemd timer.
Kubernetes or a PaaS would wrap a single short-lived process in machinery built for
fleets of long-lived ones; nothing here would use it.

## Prerequisites

- Docker (the image builds on `python:3.12-slim`; any recent engine works)
- ~1 GB free disk for the image, plus room for the log
  (the 600-card/45-day demo log is tens of MB)
- Alternatively, no Docker at all: Python ≥ 3.11 and `pip install -r
  requirements.txt` runs the same commands directly.

## Environment variables

Nothing is read from a dotenv file automatically; set these on the process.

| Variable | Required | Purpose |
|---|---|---|
| `FRAUDSHIELD_SEAL_KEY` | only for `seal` / `verify --require-seal` | HMAC key that anchors the chain head. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. Keep it **off** the host that writes the log — see SECURITY.md. |
| `FRAUDSHIELD_LOG_LEVEL` | no | JSON log verbosity on stderr. The image sets `INFO`; the bare CLI defaults to `WARNING`. |

## Migrations

Schema migrations run automatically when a log is opened: the store compares
`PRAGMA user_version` against the versions it knows and applies whatever is missing,
inside a transaction, before the first read or write. There is no separate migrate
step and no downgrade path — a log written by a newer schema than the binary knows
is refused with `schema_too_new` rather than guessed at. Back up the `.db` file
before upgrading the image if you care about rolling back (see below).

## Build

```bash
docker build -t fraudshield .
```

CI performs this same build on every push (`.github/workflows/ci.yml`, `docker`
job), so a red main means the image does not build.

## Deploy procedure

1. Build (or pull your own registry copy of) the image on the host.
2. Create the volume and ingest:

   ```bash
   docker volume create fraudshield-data
   docker run --rm -v fraudshield-data:/data fraudshield \
       build --cards 600 --days 45          # or: build --data /data/export.csv
   ```

3. Confirm health before pointing anything else at it:

   ```bash
   docker run --rm -v fraudshield-data:/data fraudshield --json health
   docker run --rm -v fraudshield-data:/data fraudshield --json health --deep
   ```

4. Seal the head so later tampering is provable (key held elsewhere):

   ```bash
   docker run --rm -e FRAUDSHIELD_SEAL_KEY -v fraudshield-data:/data fraudshield seal
   ```

5. Schedule the recurring work with cron / a systemd timer, e.g. a nightly
   `verify` + `export-jsonl` to off-host storage:

   ```bash
   docker run --rm -v fraudshield-data:/data fraudshield --json verify
   docker run --rm -v fraudshield-data:/data fraudshield export-jsonl --to /data/archive.jsonl
   ```

Analysts open the console with `docker run --rm -it -v fraudshield-data:/data
fraudshield dashboard`.

## Health checks

- `health` exits 0/1 and prints one JSON document with `--json`; it is the image's
  `HEALTHCHECK`, so `docker inspect --format '{{.State.Health.Status}}'` works on a
  long-running container.
- `health --deep` re-hashes the entire chain (same work as `verify`); run it on a
  schedule, not per-probe.
- `--metrics` on any command dumps that run's counters and latency histograms as
  JSON on stderr for whatever collects your job output.

## Rollback

The state is the log file; the code is the image tag. They roll back separately:

- **Code**: run the previous image tag. The store refuses a log whose schema is
  newer than it understands, so a code rollback across a schema migration also
  needs the log restored from before the upgrade.
- **State**: restore the `.db` file from backup (copy it while no writer is
  running, or use the JSONL archive: `import-jsonl --from archive.jsonl` rebuilds a
  log and re-checks every hash link on the way in).
- After either, `verify` — and `verify --require-seal` if you seal — before
  resuming writes.

## Troubleshooting

| Symptom | Meaning | Do |
|---|---|---|
| `health` exit 1, `openable: false` | no log at `/data/events.db` (volume not mounted, or never built) | mount the volume; run `build` |
| `error [schema_too_new]` | log written by a newer image | upgrade the image, or restore the older log |
| `error [chain_broken]` on `verify` | the file was edited or corrupted | restore from backup / `import-jsonl`; treat the host as suspect |
| `error [seal_mismatch]` on `verify` | history rewritten after sealing, or wrong key | check which key id the seal names; if the key is right, the log is not the one you sealed |
| `error [concurrency_conflict]` | two writers raced on one stream | retry; the losing write was refused, nothing was corrupted |
| `database is locked` under load | a long reader holding the file on a non-WAL filesystem mount | keep the volume on a local filesystem; network mounts break SQLite's locking |
| Console renders `?` boxes | terminal lacks Unicode | use a UTF-8 terminal; output falls back to ASCII where it can |
