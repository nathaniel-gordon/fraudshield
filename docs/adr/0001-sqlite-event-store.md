# 0001 — SQLite as the event store

Status: accepted

## Context

The log started as a JSONL file: one event per line, flushed on write. It is the
artifact everything else rests on — projections, audit trails, chargeback evidence —
so its failure modes matter more than its convenience.

## Problem

Three observed failure modes of the file version:

1. `flush()` without `fsync()` meant a power cut lost acknowledged events;
2. a kill mid-write left a torn last line that made the file permanently unopenable —
   the reader hit a raw JSON error with no recovery path;
3. two processes appending concurrently silently produced duplicate sequence numbers.

## Options considered

- **Keep JSONL, add fsync + a recovery reader.** Fixes 1 and half of 2; does nothing
  for 3 without a separate lock file, which is its own failure mode on Windows.
- **PostgreSQL.** Solves everything, plus real multi-host access — at the price of a
  running server, credentials, and an operational dependency this single-machine tool
  cannot justify. Nothing here needs a network database.
- **SQLite (stdlib), WAL, `synchronous=FULL`.** Durable commits, atomic groups, and
  `BEGIN IMMEDIATE` + `UNIQUE(stream_id, version)` turn a losing concurrent writer
  into a loud error instead of corruption. No new dependency.

## Decision

SQLite, via the stdlib `sqlite3` module. `synchronous=FULL` rather than `NORMAL`,
because an acknowledged fraud alert must survive the power going out; appends are
batched so the fsync cost is amortised per group, not per event.

## Consequences

- The record contract is unchanged (`{seq, type, prev, hash, data}`, same SHA-256
  chain), so old logs import byte-for-byte (`import-jsonl`).
- The grep-able archive the file format was originally chosen for still exists, on
  demand (`export-jsonl`).
- Single-filesystem only: SQLite's guarantees stop at the disk. Multi-host access is
  explicitly out of scope (see README limitations).
- `checkpoint --prune` and `create_fresh` are the only deletion paths, and neither
  touches `events` rows outside a fresh create.
