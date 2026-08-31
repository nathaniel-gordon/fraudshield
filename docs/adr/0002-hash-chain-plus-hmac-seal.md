# 0002 — Tamper evidence: hash chain plus an HMAC seal

Status: accepted

## Context

The log is the audit artifact. A dispute rests on "these records were not altered
after the fact", and each record already carried
`hash = SHA-256(prev | seq | type | canonical_json(data))`.

## Problem

The chain alone only catches a careless editor. Anyone who can write the database
can edit a payload and recompute every downstream hash, after which `verify()` walks
clean. Early docstrings claimed the chain proved more than it does; the suite now
pins the opposite (`test_a_fully_recomputed_chain_still_verifies`).

## Options considered

- **Chain only, documented honestly.** Zero cost, but "tamper-evident against
  careless editors" is a weak claim for a fraud log.
- **Asymmetric signatures per record.** Strongest per-record story, but key handling
  is much heavier, signing every record is slow, and the verifying side needs PKI
  this project has nowhere to put.
- **HMAC seal over the head.** One `HMAC-SHA256(key, seq | head_hash | count)` row
  anchors the whole prefix. Cheap, stdlib-only (`hmac`, `hashlib`), and the secret
  lives wherever the operator keeps secrets — outside the database.

## Decision

Periodic HMAC seals (`sentinel seal`), verified by `verify` when a key is present
and required by `verify --require-seal`. The key comes from `FRAUDSHIELD_SEAL_KEY`;
the seal row stores a key *id* derived from the key, never the key.

## Consequences

- Rewriting sealed history is detectable by anyone holding the key.
- The honest limits are documented where the code is: a key stored next to the log
  proves nothing; seal rows can be deleted by a writer (visibly — the operator holds
  the receipts); records after the newest seal are protected by the chain alone.
- Sealing is an operator action, not automatic: the value of a seal is exactly the
  independence of where its key and receipt live.
