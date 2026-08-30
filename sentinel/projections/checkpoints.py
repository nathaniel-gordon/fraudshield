"""Saved read-model state, so a rebuild does not always start at sequence 1.

Rebuilding from zero is the ground truth and stays the default. But every time-travel
view the console offers folds the whole prefix, and on a log of any size an analyst
pays that on each keystroke. A checkpoint is the fold already done up to some
sequence number: restore it, apply what came after, and the answer is identical.

Three rules keep that "identical" honest, because a checkpoint is the one place where
a wrong answer could be returned quickly and confidently:

1. **A checkpoint covers the whole read model or none of it.** One row, one JSON
   object, every projection inside it. There is no way to assemble a half-checkpoint
   from a projection that was saved at a different sequence number.
2. **It is bound to the log it came from.** The row stores the chain hash at its own
   sequence number. Restore refuses if the log's hash there differs, so a checkpoint
   can never leak across a re-imported, re-built or tampered log.
3. **It is verified after loading, not trusted.** The stored fingerprint is compared
   against the restored model's own. A mismatch — corrupted JSON, or projection code
   that has changed shape since the row was written — discards the checkpoint and
   falls back to a full replay.

The state is JSON, never pickle. A checkpoint is data the process reads back into
itself, and making that a code-execution path would undo the point of the exercise.
"""
from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import CheckpointUnusable
from ..obs import get_logger
from ..store.sqlite_store import EventStore
from .registry import ReadModel

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """One whole read model, frozen at one sequence number."""

    seq: int
    chain_hash: str
    fingerprint: str
    applied: int
    names: tuple[str, ...]
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "chain_hash": self.chain_hash,
            "fingerprint": self.fingerprint,
            "applied": self.applied,
            "names": list(self.names),
            "created_at": round(self.created_at, 3),
        }


class CheckpointStore:
    """Reads and writes the ``projection_checkpoints`` table of one event store."""

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._con = store.connection

    # ------------------------------------------------------------------ write

    def save(self, model: ReadModel) -> Checkpoint:
        """Persist ``model``'s full state at the sequence number it has folded to.

        Raises:
            CheckpointUnusable: the model is empty, or its sequence number is not on
                the log — either way there is nothing a later restore could anchor to.
        """
        seq = model.upto_seq
        if seq <= 0:
            raise CheckpointUnusable("cannot checkpoint an empty read model", seq=seq)
        chain_hash = self._store.hash_at(seq)
        if chain_hash is None:
            raise CheckpointUnusable("no record at that sequence number", seq=seq)

        names = tuple(sorted(model.names()))
        state = {name: model[name].checkpoint() for name in names}
        checkpoint = Checkpoint(
            seq=seq,
            chain_hash=chain_hash,
            fingerprint=model.fingerprint(),
            applied=model.applied,
            names=names,
            created_at=time.time(),
        )
        self._con.execute(
            "INSERT OR REPLACE INTO projection_checkpoints"
            " (seq, chain_hash, fingerprint, applied, names, state, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                checkpoint.seq,
                checkpoint.chain_hash,
                checkpoint.fingerprint,
                checkpoint.applied,
                json.dumps(list(names)),
                json.dumps(state, separators=(",", ":"), sort_keys=True),
                checkpoint.created_at,
            ),
        )
        self._con.commit()
        log.info(
            "checkpoint_saved",
            extra={"seq": seq, "applied": checkpoint.applied, "projections": len(names)},
        )
        return checkpoint

    def prune(self, keep: int = 3) -> int:
        """Drop all but the ``keep`` newest checkpoints. Returns how many went.

        Deleting a checkpoint destroys no facts — the log is untouched and every one
        of them can be recomputed. This is the only DELETE in the codebase, and it is
        deliberately not on a table that holds events.
        """
        if keep < 0:
            raise ValueError("keep must not be negative")
        cursor = self._con.execute(
            "DELETE FROM projection_checkpoints WHERE seq NOT IN ("
            " SELECT seq FROM projection_checkpoints ORDER BY seq DESC LIMIT ?)",
            (keep,),
        )
        removed = int(cursor.rowcount or 0)
        self._con.commit()
        if removed:
            log.info("checkpoints_pruned", extra={"removed": removed, "kept": keep})
        return removed

    # ------------------------------------------------------------------- read

    def list(self, limit: int = 50, offset: int = 0) -> list[Checkpoint]:
        """Newest first, paged — the table is small but it is still a list."""
        rows = self._con.execute(
            "SELECT seq, chain_hash, fingerprint, applied, names, created_at"
            " FROM projection_checkpoints ORDER BY seq DESC LIMIT ? OFFSET ?",
            (max(0, limit), max(0, offset)),
        ).fetchall()
        return [_to_checkpoint(row) for row in rows]

    def count(self) -> int:
        return int(self._con.execute("SELECT COUNT(*) FROM projection_checkpoints").fetchone()[0])

    def latest_at_or_before(self, seq: int) -> Checkpoint | None:
        """The newest checkpoint usable as a starting point for a fold up to ``seq``."""
        row = self._con.execute(
            "SELECT seq, chain_hash, fingerprint, applied, names, created_at"
            " FROM projection_checkpoints WHERE seq <= ? ORDER BY seq DESC LIMIT 1",
            (seq,),
        ).fetchone()
        return None if row is None else _to_checkpoint(row)

    def load_into(self, model: ReadModel, upto_seq: int) -> Checkpoint | None:
        """Restore the newest usable checkpoint at or before ``upto_seq`` into ``model``.

        Returns the checkpoint used, or ``None`` when none was usable and the caller
        should fold from zero. Unusable is a normal outcome, not an error: a fresh
        database has no checkpoints, and one written against a different log is
        supposed to be ignored.
        """
        wanted = set(model.names())
        row = self._con.execute(
            "SELECT seq, chain_hash, fingerprint, applied, names, created_at, state"
            " FROM projection_checkpoints WHERE seq <= ? ORDER BY seq DESC LIMIT 1",
            (upto_seq,),
        ).fetchone()
        if row is None:
            return None
        candidate = _to_checkpoint(row[:6])

        if set(candidate.names) != wanted:
            _discard(candidate, "projection set differs from this read model")
            return None
        if self._store.hash_at(candidate.seq) != candidate.chain_hash:
            # The log changed under the checkpoint: re-imported, rebuilt, or edited.
            _discard(candidate, "chain hash at that sequence does not match the log")
            return None
        try:
            state = json.loads(row[6])
            for name in candidate.names:
                model[name].restore(state[name])
        except (ValueError, KeyError, TypeError) as exc:
            _discard(candidate, f"state did not load: {exc}")
            return None

        model.applied = candidate.applied
        model.upto_seq = candidate.seq
        if model.fingerprint() != candidate.fingerprint:
            _discard(candidate, "restored fingerprint does not match the recorded one")
            return None
        return candidate


def _discard(candidate: Checkpoint, reason: str) -> None:
    """Log why a checkpoint was skipped. Always recoverable by replaying from zero."""
    log.warning("checkpoint_discarded", extra={"seq": candidate.seq, "reason": reason})


def _to_checkpoint(row: Sequence[Any]) -> Checkpoint:
    names = json.loads(row[4])
    return Checkpoint(
        seq=int(row[0]),
        chain_hash=str(row[1]),
        fingerprint=str(row[2]),
        applied=int(row[3]),
        names=tuple(names),
        created_at=float(row[5]),
    )


def rebuild_with_checkpoints(store: EventStore, upto_seq: int | None = None) -> ReadModel:
    """``ReadModel.rebuild`` that resumes from a checkpoint when one is usable.

    Always returns the same read model a from-zero rebuild would, which is what makes
    it safe to reach for by default. ``ReadModel.resumed_from`` says whether a
    checkpoint was actually used, and is 0 when the fold started at the beginning.
    """
    target = upto_seq if upto_seq is not None else store.last_seq
    model = ReadModel()
    used = CheckpointStore(store).load_into(model, target)
    if used is None:
        # A rejected checkpoint may have restored some projections before the
        # fingerprint check failed, so the fallback needs a clean model rather than
        # this half-populated one. Folding onto residue would double-count it.
        return ReadModel().apply_all(store.read(upto_seq=upto_seq))
    model.resumed_from = used.seq
    return model.apply_all(store.read(from_seq=used.seq + 1, upto_seq=upto_seq))


__all__ = ["Checkpoint", "CheckpointStore", "rebuild_with_checkpoints"]
