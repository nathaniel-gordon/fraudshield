# 0004 — Read-model checkpoints are a disposable, verified cache

Status: accepted

## Context

Every projection is a fold over the log, and a from-zero fold is the ground truth.
On the demo-sized log a full rebuild costs on the order of a second, and the
console's time-travel view folds a prefix per keystroke.

## Problem

Caching a fold is the classic place an event-sourced system starts quietly lying: a
stale or foreign snapshot returns a wrong answer quickly and confidently.

## Options considered

- **No checkpoints; always fold from 1.** Honest and simple; cost grows linearly
  with the log forever.
- **Per-projection snapshots.** Smaller writes, but permits a frankenstate assembled
  from projections saved at different sequence numbers.
- **One whole-read-model checkpoint, verified on load.** All projections in one row
  at one sequence number, bound and checked.

## Decision

`projection_checkpoints` stores the whole read model as one JSON row (never pickle —
a checkpoint the process reads back into itself must not be a code-execution path),
with three rules:

1. all-or-nothing — one row covers every projection at one `seq`;
2. bound to its log — the row stores the chain hash at its own seq, and restore
   refuses if the log's hash there differs;
3. verified after load — the stored fingerprint is recomputed from the restored
   model; any mismatch discards the checkpoint and falls back to a full replay.

## Consequences

- `rebuild_with_checkpoints` is safe as the default everywhere the console opens a
  log: the worst case is the cost of a full fold, never a wrong answer.
- Deleting checkpoints is always safe (`checkpoint --prune`), which keeps them
  firmly in "cache" territory — the only deletable table besides seals.
- Checkpoints are written at build time and on demand; there is no periodic policy
  (a known limitation, stated in the README).
