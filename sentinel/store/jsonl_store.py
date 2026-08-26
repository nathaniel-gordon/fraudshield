"""The append-only event log.

A single JSONL file, opened for append and never for update. There is intentionally
no ``update``, ``delete`` or ``truncate`` method: the absence of those verbs is the
architectural guarantee. Everything downstream — every projection, every metric on
the dashboard, every counterfactual — is a fold over what this file returns.

JSONL rather than SQLite because the log is the archival artifact: an auditor with
``grep`` and ``python -m json.tool`` can read it in ten years without our code.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Iterator

from ..domain.events import DomainEvent
from .codec import GENESIS_HASH, ChainBroken, Recorded, decode, encode


class EventStore:
    """Append-only, hash-chained JSONL event log.

    The writer keeps the file handle open and flushes after every append batch so
    that a reader (``read``/``__iter__``, which open the file separately) always
    sees a complete prefix — important on Windows where the same path cannot be
    reopened for writing while buffered data is pending.
    """

    def __init__(self, path: str | os.PathLike[str], *, append: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not append and self.path.exists():
            self.path.unlink()
        self._last_seq, self._head_hash = self._scan_head()
        self._fh = self.path.open("a", encoding="utf-8", newline="\n")

    # ---------------------------------------------------------------- writing

    def append(self, event: DomainEvent) -> Recorded:
        """Append a single fact and return its recorded form."""
        return self.append_many([event])[0]

    def append_many(self, events: Iterable[DomainEvent]) -> list[Recorded]:
        """Append a batch atomically enough for our purposes: one flush at the end.

        Batching matters — the demo writes ~78k events, and a flush per event
        would dominate the runtime.
        """
        records: list[Recorded] = []
        lines: list[str] = []
        seq, prev = self._last_seq, self._head_hash
        for event in events:
            seq += 1
            line, rec = encode(seq, event, prev)
            prev = rec.hash
            lines.append(line)
            records.append(rec)
        if not records:
            return records
        self._fh.write("\n".join(lines) + "\n")
        self._fh.flush()
        self._last_seq, self._head_hash = seq, prev
        return records

    # ---------------------------------------------------------------- reading

    def read(self, upto_seq: int | None = None, from_seq: int = 1) -> Iterator[Recorded]:
        """Stream records in log order.

        Args:
            upto_seq: inclusive upper bound — this is the time-travel primitive.
            from_seq: inclusive lower bound, for incremental tailing.

        Yields records one at a time rather than materialising a list: a
        multi-hundred-megabyte production log must still replay in constant memory.
        """
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = decode(line)
                if rec.seq < from_seq:
                    continue
                if upto_seq is not None and rec.seq > upto_seq:
                    return
                yield rec

    def __iter__(self) -> Iterator[Recorded]:
        return self.read()

    def __len__(self) -> int:
        return self._last_seq

    @property
    def last_seq(self) -> int:
        """Sequence number of the newest fact; 0 for an empty log."""
        return self._last_seq

    @property
    def head_hash(self) -> str:
        """Chain head — two logs with the same head hold identical history."""
        return self._head_hash

    # ------------------------------------------------------------ integrity

    def verify(self) -> int:
        """Walk the whole chain and confirm nothing was altered.

        Returns:
            The number of records verified.

        Raises:
            ChainBroken: on a hash mismatch, a gap in the sequence, or a record
                whose ``prev`` does not match the previous record's hash.
        """
        expected_seq, prev = 1, GENESIS_HASH
        count = 0
        for rec in self.read():
            if rec.seq != expected_seq:
                raise ChainBroken(f"sequence gap: expected {expected_seq}, found {rec.seq}")
            if rec.prev_hash != prev:
                raise ChainBroken(f"record {rec.seq} does not link to record {rec.seq - 1}")
            prev = rec.hash
            expected_seq += 1
            count += 1
        return count

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Close the write handle. Safe to call twice; required on Windows."""
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- private

    def _scan_head(self) -> tuple[int, str]:
        """Recover the append position from an existing log, so runs can resume."""
        if not self.path.exists():
            return 0, GENESIS_HASH
        seq, prev = 0, GENESIS_HASH
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = decode(line)
                    seq, prev = rec.seq, rec.hash
        return seq, prev


__all__ = ["EventStore", "Recorded", "ChainBroken", "GENESIS_HASH"]
