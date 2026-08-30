"""JSONL export and import — the archival format.

The database is the operational log; this is the copy you hand to somebody who has
``grep`` and ten years but not this codebase. One record per line, exactly the shape
:mod:`sentinel.store.codec` defines, so the chain can be verified with nothing but a
SHA-256 implementation.

Import is deliberately forgiving in one specific way and unforgiving in every other:
a *truncated final line* — what a kill during a write to the old file-based log
produced — can be dropped with ``recover=True``, because a partial record is not a
record. Anything else (a bad hash, a gap, a broken link) aborts the import, because
those are edits, not accidents.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..errors import ChainBroken, LogCorrupt
from ..obs import get_logger
from .codec import GENESIS_HASH, Recorded, decode, encode
from .sqlite_store import EventStore

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ImportReport:
    """What an import did, including anything it had to leave behind."""

    imported: int
    skipped_tail_bytes: int
    head_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "imported": self.imported,
            "skipped_tail_bytes": self.skipped_tail_bytes,
            "head_hash": self.head_hash,
        }


def export_jsonl(store: EventStore, path: str | os.PathLike[str]) -> int:
    """Write every record to ``path`` as JSONL. Returns the number written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("w", encoding="utf-8", newline="\n") as fh:
        for rec in store.read():
            line, _ = encode(rec.seq, rec.event, rec.prev_hash)
            fh.write(line + "\n")
            written += 1
        fh.flush()
        os.fsync(fh.fileno())
    log.info("jsonl_exported", extra={"records": written, "path": str(target)})
    return written


def read_jsonl(
    path: str | os.PathLike[str], *, recover: bool = False
) -> Iterator[Recorded]:
    """Stream records out of a JSONL archive, checking each one's own hash.

    Args:
        recover: drop an incomplete final line instead of raising. Only the *last*
            line may be dropped — a torn record anywhere else means the file was
            edited, not interrupted.
    """
    source = Path(path)
    with source.open("r", encoding="utf-8") as fh:
        # One line of lookahead, so a decode failure can tell "torn tail" from "edited
        # in the middle" without holding the file in memory.
        held: str | None = None
        for raw in fh:
            text = raw.strip()
            if not text:
                continue
            if held is not None:
                yield decode(held)
            held = text
        if held is None:
            return
        try:
            yield decode(held)
        except LogCorrupt:
            if not recover:
                raise
            log.warning("jsonl_tail_dropped", extra={"bytes": len(held), "path": str(source)})


def import_jsonl(
    path: str | os.PathLike[str], store: EventStore, *, recover: bool = False, batch: int = 2000
) -> ImportReport:
    """Load a JSONL archive into an empty store, re-checking the chain as it goes.

    Raises:
        ChainBroken: the archive's own links do not hold, or the destination store is
            not empty (importing into a populated log would renumber the archive).
    """
    if store.last_seq:
        raise ChainBroken(
            "import needs an empty log; the destination already holds records",
            last_seq=store.last_seq,
        )
    source = Path(path)
    size = source.stat().st_size
    expected_seq, prev = 1, GENESIS_HASH
    pending: list[Recorded] = []
    imported = 0
    consumed = 0

    def flush() -> None:
        nonlocal imported
        if not pending:
            return
        store.append_many([rec.event for rec in pending])
        imported += len(pending)
        pending.clear()

    for rec in read_jsonl(source, recover=recover):
        if rec.seq != expected_seq:
            raise ChainBroken(
                f"archive sequence gap: expected {expected_seq}, found {rec.seq}",
                expected=expected_seq,
                found=rec.seq,
            )
        if rec.prev_hash != prev:
            raise ChainBroken(f"archive record {rec.seq} does not link to its predecessor",
                              seq=rec.seq)
        pending.append(rec)
        prev = rec.hash
        expected_seq += 1
        consumed += len(_line_of(rec).encode("utf-8")) + 1
        if len(pending) >= batch:
            flush()
    flush()
    if store.head_hash != prev:  # pragma: no cover - guarded by the checks above
        raise ChainBroken("re-appended log does not reproduce the archive's head hash")
    return ImportReport(
        imported=imported, skipped_tail_bytes=max(0, size - consumed), head_hash=store.head_hash
    )


def _line_of(rec: Recorded) -> str:
    line, _ = encode(rec.seq, rec.event, rec.prev_hash)
    return line


__all__ = ["ImportReport", "export_jsonl", "import_jsonl", "read_jsonl"]
