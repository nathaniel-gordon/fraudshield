"""Anchoring the chain head, so "nobody edited this" is a claim we can defend.

A hash chain on its own is tamper-*evident* only against a careless editor. Anyone who
can write the log can change a payload and recompute every downstream hash; the walk
then verifies clean. That was true of this log until this module existed, and the
docstrings that claimed otherwise were wrong.

A seal fixes it with the one ingredient the chain lacks: a secret the writer does not
have. ``seal()`` records ``HMAC-SHA256(key, seq|head_hash|count)`` for the current
head. Rewriting history changes the hash at that sequence number, and the MAC cannot
be recomputed without the key, so :func:`verify_seals` reports the mismatch.

The honest limits, stated once:

* the key must live outside the database — an operator env var, a secret manager —
  otherwise this proves nothing at all;
* an attacker with write access can *delete* seal rows. Deletion is visible (the log
  claims fewer seals than the operator archived), which is why ``seal`` prints the
  receipt it just wrote and ``verify --require-seal`` fails when there is none;
* seals cover a prefix. Records appended after the newest seal are protected by the
  chain alone until the next seal.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any

from ..errors import SealMismatch
from ..obs import get_logger
from .sqlite_store import EventStore

log = get_logger(__name__)

ENV_KEY = "FRAUDSHIELD_SEAL_KEY"


@dataclass(frozen=True, slots=True)
class Seal:
    """One anchored head."""

    seq: int
    head_hash: str
    record_count: int
    key_id: str
    mac: str
    sealed_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "head_hash": self.head_hash,
            "record_count": self.record_count,
            "key_id": self.key_id,
            "mac": self.mac,
            "sealed_at": round(self.sealed_at, 3),
        }


@dataclass(frozen=True, slots=True)
class SealCheck:
    """Result of checking every seal on a log against the chain it covers."""

    checked: int
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"seals_checked": self.checked, "ok": self.ok, "detail": self.detail}


def load_key(raw: str | None = None) -> bytes:
    """Resolve the sealing key from an argument or the environment.

    Raises:
        SealMismatch: no key available. Sealing without one would be theatre.
    """
    material = raw if raw is not None else os.environ.get(ENV_KEY, "")
    if not material:
        raise SealMismatch(
            f"no sealing key: pass --key or set {ENV_KEY} (see .env.example)", env_var=ENV_KEY
        )
    return material.encode("utf-8")


def key_id(key: bytes) -> str:
    """Short, non-reversible identifier so a log can say *which* key sealed it."""
    return hashlib.sha256(b"fraudshield-key-id|" + key).hexdigest()[:12]


def compute_mac(key: bytes, seq: int, head_hash: str, record_count: int) -> str:
    material = f"{seq}|{head_hash}|{record_count}".encode()
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def seal(store: EventStore, key: bytes, *, now: float | None = None) -> Seal:
    """Anchor the log's current head. Re-sealing the same head is idempotent.

    Raises:
        SealMismatch: the log is empty, or a different key already sealed this head.
    """
    if store.last_seq == 0:
        raise SealMismatch("refusing to seal an empty log")
    count = store.count()
    entry = Seal(
        seq=store.last_seq,
        head_hash=store.head_hash,
        record_count=count,
        key_id=key_id(key),
        mac=compute_mac(key, store.last_seq, store.head_hash, count),
        sealed_at=float(now if now is not None else time.time()),
    )
    con = store.connection
    existing = con.execute("SELECT mac, key_id FROM seals WHERE seq = ?", (entry.seq,)).fetchone()
    if existing is not None and (existing[0] != entry.mac or existing[1] != entry.key_id):
        raise SealMismatch(
            "this sequence is already sealed with a different key or head", seq=entry.seq
        )
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            "INSERT OR REPLACE INTO seals (seq, head_hash, record_count, key_id, mac, sealed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (entry.seq, entry.head_hash, entry.record_count, entry.key_id, entry.mac,
             entry.sealed_at),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    log.info("log_sealed", extra={"seq": entry.seq, "key_id": entry.key_id})
    return entry


def seals(store: EventStore) -> list[Seal]:
    """Every seal on the log, oldest first."""
    rows = store.connection.execute(
        "SELECT seq, head_hash, record_count, key_id, mac, sealed_at FROM seals ORDER BY seq"
    ).fetchall()
    return [Seal(int(r[0]), str(r[1]), int(r[2]), str(r[3]), str(r[4]), float(r[5])) for r in rows]


def verify_seals(store: EventStore, key: bytes, *, require: bool = False) -> SealCheck:
    """Check every seal against the chain and against the key.

    A rewritten history is caught here even when :meth:`EventStore.verify` passes,
    because the recomputed head hash at the sealed sequence no longer matches the MAC.

    Raises:
        SealMismatch: a seal fails, or ``require`` is set and there are none.
    """
    recorded = seals(store)
    if not recorded:
        if require:
            raise SealMismatch("log carries no seal", seq=store.last_seq)
        return SealCheck(0, True, "log carries no seal")
    kid = key_id(key)
    for entry in recorded:
        if entry.key_id != kid:
            raise SealMismatch(
                f"seal at {entry.seq} was written with key {entry.key_id}, not {kid}",
                seq=entry.seq,
                key_id=entry.key_id,
            )
        actual_head = store.hash_at(entry.seq)
        if actual_head is None:
            raise SealMismatch(
                f"seal at {entry.seq} covers a record the log no longer holds", seq=entry.seq
            )
        expected = compute_mac(key, entry.seq, actual_head, entry.record_count)
        if not hmac.compare_digest(expected, entry.mac):
            raise SealMismatch(
                f"log does not match its seal at record {entry.seq} — history was rewritten",
                seq=entry.seq,
                sealed_head=entry.head_hash,
                actual_head=actual_head,
            )
    newest = recorded[-1]
    return SealCheck(
        len(recorded),
        True,
        f"{len(recorded)} seal(s) verify; newest covers records 1..{newest.seq}",
    )


__all__ = [
    "ENV_KEY",
    "Seal",
    "SealCheck",
    "compute_mac",
    "key_id",
    "load_key",
    "seal",
    "seals",
    "verify_seals",
]
