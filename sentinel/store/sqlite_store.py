"""The append-only event log, stored in SQLite.

Why a database and not the JSONL file this used to be: the file was flushed but never
fsynced, so a power cut lost acknowledged events, and a kill mid-write left a torn last
line that made the log *permanently unopenable* — the reader hit a raw JSON error and
there was no path back. Two processes appending to it silently produced duplicate
sequence numbers. None of that is acceptable for the artifact a chargeback dispute
rests on.

SQLite with WAL and ``synchronous=FULL`` gives all three back: a commit is durable, a
crash mid-append rolls the whole group back rather than tearing it, and the write lock
plus ``UNIQUE(stream_id, version)`` make a losing writer fail loudly instead of
corrupting the chain. The grep-able archive the old format was chosen for is still
available on demand — see :mod:`sentinel.store.jsonl`.

What has *not* changed is the record contract or the hash chain: the same
``{seq, type, prev, hash, data}`` shape, the same SHA-256 over
``prev|seq|type|canonical_json(data)``. Existing logs export and import byte-for-byte.

There is still no ``update``, ``delete`` or ``truncate`` verb. Destroying a log is
possible in exactly one place, :meth:`EventStore.create_fresh`, which says so in its
name.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.events import DomainEvent, index_keys, stream_id_for
from ..errors import ChainBroken, ConcurrencyConflict, LogCorrupt, StoreError
from ..metrics import counter, histogram
from ..obs import get_logger
from .codec import (
    GENESIS_HASH,
    Recorded,
    canonical_json,
    chain_hash_text,
    event_from_data,
)
from .schema import SCHEMA_VERSION, current_version, migrate

log = get_logger(__name__)

_SELECT = "SELECT seq, type, prev_hash, hash, data FROM events"
#: SQLite's default parameter cap is far higher, but chunking the IN lists keeps the
#: query planner happy and the statement cache small.
_IN_CHUNK = 400


@dataclass(slots=True)
class CommandGroup:
    """The events one command produced, with the id that makes it idempotent.

    ``command_id`` empty means "do not record this group for deduplication" — used by
    the low-level append helpers and by tests.
    """

    events: Sequence[DomainEvent]
    command_id: str = ""
    command_type: str = ""
    first_seq: int = field(default=0, init=False)
    last_seq: int = field(default=0, init=False)


class DuplicateCommand(StoreError):
    """This command id is already recorded; its events are on the log already."""

    code = "duplicate_command"

    def __init__(self, message: str, *, command_id: str, first_seq: int, last_seq: int) -> None:
        super().__init__(message, command_id=command_id, first_seq=first_seq, last_seq=last_seq)
        self.command_id = command_id
        self.range = (first_seq, last_seq)


class EventStore:
    """Append-only, hash-chained event log on SQLite.

    One instance owns one connection. Appends take ``BEGIN IMMEDIATE`` so the chain
    head is read and extended under the database's write lock — the reason two
    processes can share a log without producing duplicate sequence numbers.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timeout: float = 30.0,
        synchronous: str = "FULL",
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(
            str(self.path), timeout=timeout, isolation_level=None, check_same_thread=False
        )
        self._con.execute("PRAGMA journal_mode = WAL")
        # FULL, not NORMAL: an acknowledged fraud alert must survive the power going
        # out, and the log is written in batches so the fsync cost is amortised.
        self._con.execute(f"PRAGMA synchronous = {'FULL' if synchronous.upper() == 'FULL' else 'NORMAL'}")
        self._con.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        applied = migrate(self._con)
        if applied:
            log.info("schema_migrated", extra={"applied": applied, "version": SCHEMA_VERSION})
        self._last_seq, self._head_hash = self._read_head()
        self._versions: dict[str, int] = {}
        self._closed = False

    # -------------------------------------------------------------- lifecycle

    @classmethod
    def create_fresh(cls, path: str | os.PathLike[str], **kwargs: Any) -> EventStore:
        """Delete any existing log at ``path`` and return an empty one.

        The only destructive entry point in the package, and it is a named
        constructor rather than a flag on ``__init__`` so it cannot be reached by
        accident.
        """
        target = Path(path)
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(target) + suffix)
            if candidate.exists():
                candidate.unlink()
        return cls(target, **kwargs)

    def close(self) -> None:
        """Close the connection. Safe to call twice."""
        if not self._closed:
            # Folding the WAL back in is a courtesy to whoever copies the file next,
            # not a correctness requirement: an un-checkpointed WAL replays on open.
            with contextlib.suppress(sqlite3.Error):
                self._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._con.close()
            self._closed = True

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- writing

    def append(self, event: DomainEvent) -> Recorded:
        """Append a single fact and return its recorded form."""
        return self.append_many([event])[0]

    def append_many(
        self,
        events: Iterable[DomainEvent],
        *,
        command_id: str = "",
        command_type: str = "",
        expected_versions: Mapping[str, int] | None = None,
    ) -> list[Recorded]:
        """Append one group of facts atomically.

        Args:
            command_id: when given, recorded in ``processed_commands`` so a repeat
                submit is rejected rather than duplicated.
            expected_versions: optimistic concurrency — ``{stream_id: version}`` the
                caller believes is current. A mismatch raises
                :class:`ConcurrencyConflict` and nothing is written.
        """
        group = CommandGroup(list(events), command_id, command_type)
        return self.append_groups([group], expected_versions=expected_versions)

    def append_groups(
        self,
        groups: Sequence[CommandGroup],
        *,
        expected_versions: Mapping[str, int] | None = None,
    ) -> list[Recorded]:
        """Append several command groups in one transaction.

        Bulk ingestion submits thousands of commands; one transaction per chunk keeps
        the fsync count proportional to chunks rather than to events, while still
        making every chunk all-or-nothing.

        Each group's ``first_seq``/``last_seq`` are filled in on the way through, so
        the caller can record what a command produced.
        """
        flat = [ev for group in groups for ev in group.events]
        if not flat:
            return []
        with histogram("store.append_ms").time(), self._lock:
            con = self._con
            con.execute("BEGIN IMMEDIATE")
            try:
                seq, prev = self._read_head()
                self._check_expected_versions(expected_versions)
                versions = self._stream_versions({stream_id_for(ev) for ev in flat})
                records: list[Recorded] = []
                rows: list[tuple[Any, ...]] = []
                for group in groups:
                    group.first_seq = seq + 1
                    for event in group.events:
                        seq += 1
                        stream = stream_id_for(event)
                        versions[stream] = versions.get(stream, 0) + 1
                        data_json = canonical_json(event.to_data())
                        digest = chain_hash_text(prev, seq, event.type, data_json)
                        txn_id, case_id = index_keys(event)
                        rows.append(
                            (seq, stream, versions[stream], event.type, prev, digest,
                             data_json, txn_id, case_id)
                        )
                        records.append(Recorded(seq, event, prev, digest))
                        prev = digest
                    group.last_seq = seq
                con.executemany(
                    "INSERT INTO events (seq, stream_id, version, type, prev_hash, hash,"
                    " data, txn_id, case_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                recorded_commands = [
                    (g.command_id, g.command_type or "?", g.first_seq, g.last_seq)
                    for g in groups
                    if g.command_id and g.events
                ]
                if recorded_commands:
                    con.executemany(
                        "INSERT INTO processed_commands (command_id, command_type,"
                        " first_seq, last_seq) VALUES (?, ?, ?, ?)",
                        recorded_commands,
                    )
                con.commit()
            except sqlite3.IntegrityError as exc:
                con.rollback()
                raise self._translate_integrity_error(exc, groups) from exc
            except Exception:
                con.rollback()
                raise
            self._last_seq, self._head_hash = seq, prev
            self._versions.update(versions)
        counter("store.events_appended").inc(len(records))
        log.debug(
            "events_committed",
            extra={"count": len(records), "first_seq": records[0].seq, "last_seq": seq},
        )
        return records

    def _check_expected_versions(self, expected: Mapping[str, int] | None) -> None:
        if not expected:
            return
        for stream, want in expected.items():
            found = self._stream_version_uncached(stream)
            if found != want:
                raise ConcurrencyConflict(
                    f"stream {stream!r} is at version {found}, expected {want}",
                    stream_id=stream,
                    expected=want,
                    found=found,
                )

    def _translate_integrity_error(
        self, exc: sqlite3.IntegrityError, groups: Sequence[CommandGroup]
    ) -> StoreError:
        """Turn a constraint violation into the error that explains what happened."""
        text = str(exc)
        if "processed_commands" in text:
            for group in groups:
                seen = self.command_range(group.command_id) if group.command_id else None
                if seen:
                    return DuplicateCommand(
                        f"command {group.command_id} was already applied",
                        command_id=group.command_id,
                        first_seq=seen[0],
                        last_seq=seen[1],
                    )
            return DuplicateCommand(
                "a command in this batch was already applied", command_id="", first_seq=0, last_seq=0
            )
        if "ux_events_txn_observed" in text or "events.txn_id" in text:
            return ConcurrencyConflict(
                "a TransactionObserved for this txn_id is already on the log", constraint=text
            )
        if "events.stream_id" in text or "stream_id, version" in text:
            return ConcurrencyConflict(
                "another writer advanced this stream between read and write", constraint=text
            )
        return StoreError(f"append violated a storage constraint: {text}")

    # ---------------------------------------------------------------- reading

    def read(
        self,
        upto_seq: int | None = None,
        from_seq: int = 1,
        *,
        types: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> Iterator[Recorded]:
        """Stream records in log order.

        Args:
            upto_seq: inclusive upper bound — this is the time-travel primitive.
            from_seq: inclusive lower bound, for incremental tailing.
            types: restrict to these event type names (uses the ``(type, seq)`` index).
            limit: stop after this many records.

        Yields records one at a time straight off a cursor, so replaying a log far
        larger than memory costs one row at a time.
        """
        sql = f"{_SELECT} WHERE seq >= ?"
        params: list[Any] = [from_seq]
        if upto_seq is not None:
            sql += " AND seq <= ?"
            params.append(upto_seq)
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            params.extend(types)
        sql += " ORDER BY seq"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cursor = self._con.execute(sql, params)
        try:
            for row in cursor:
                yield _to_record(row)
        finally:
            cursor.close()

    def __iter__(self) -> Iterator[Recorded]:
        return self.read()

    def __len__(self) -> int:
        return self._last_seq

    def count(self) -> int:
        """Number of records actually stored (equals ``last_seq`` on an intact log)."""
        return int(self._con.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    @property
    def last_seq(self) -> int:
        """Sequence number of the newest fact; 0 for an empty log."""
        return self._last_seq

    @property
    def head_hash(self) -> str:
        """Chain head — two logs with the same head hold identical history."""
        return self._head_hash

    @property
    def schema_version(self) -> int:
        return current_version(self._con)

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying connection, for the two modules that own a sibling table.

        :mod:`sentinel.store.seal` owns ``seals`` and
        :mod:`sentinel.projections.checkpoints` owns ``projection_checkpoints``; both
        live in this schema and neither holds events. Nothing else should reach for
        this: the log's contract is the append/read/verify surface above, and the
        dependency arrow still points inward — those modules import the store, never
        the other way round.
        """
        return self._con

    def header(self) -> Recorded | None:
        """The log's self-describing first record, or ``None`` if it is empty."""
        return next(self.read(limit=1), None)

    def hash_at(self, seq: int) -> str | None:
        """The chain hash of one record, without decoding its payload."""
        row = self._con.execute("SELECT hash FROM events WHERE seq = ?", (seq,)).fetchone()
        return None if row is None else str(row[0])

    def records_between(self, first_seq: int, last_seq: int) -> list[Recorded]:
        """Every record in an inclusive sequence range."""
        return list(self.read(from_seq=first_seq, upto_seq=last_seq))

    # ------------------------------------------------------- indexed lookups

    def records_for_txn(self, txn_id: int) -> list[Recorded]:
        """Every record naming one transaction, in log order — an index hit, not a scan."""
        cursor = self._con.execute(
            f"{_SELECT} WHERE txn_id = ? ORDER BY seq", (int(txn_id),)
        )
        return [_to_record(row) for row in cursor]

    def records_for_case(self, case_id: str) -> list[Recorded]:
        """Every record naming one case id, in log order."""
        cursor = self._con.execute(f"{_SELECT} WHERE case_id = ? ORDER BY seq", (case_id,))
        return [_to_record(row) for row in cursor]

    def seq_of_case(self, case_id: str) -> int | None:
        """Where in the log a case was opened."""
        row = self._con.execute(
            "SELECT seq FROM events WHERE case_id = ? AND type = 'CaseOpened' ORDER BY seq LIMIT 1",
            (case_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def txn_of_case(self, case_id: str) -> int | None:
        """The transaction a case was opened on."""
        row = self._con.execute(
            "SELECT txn_id FROM events WHERE case_id = ? AND type = 'CaseOpened' LIMIT 1",
            (case_id,),
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def type_counts(self) -> dict[str, int]:
        """How many records of each type — one grouped index scan."""
        return {
            str(row[0]): int(row[1])
            for row in self._con.execute("SELECT type, COUNT(*) FROM events GROUP BY type")
        }

    def stream_version(self, stream_id: str) -> int:
        """Current version of one stream; 0 when it holds no facts yet."""
        return self._stream_version_uncached(stream_id)

    def streams(self, prefix: str = "") -> list[str]:
        """Stream ids, optionally restricted to a prefix such as ``card:``."""
        if prefix:
            cursor = self._con.execute(
                "SELECT DISTINCT stream_id FROM events WHERE stream_id LIKE ? ORDER BY stream_id",
                (f"{prefix}%",),
            )
        else:
            cursor = self._con.execute("SELECT DISTINCT stream_id FROM events ORDER BY stream_id")
        return [str(row[0]) for row in cursor]

    # ------------------------------------------------------------ idempotency

    def command_range(self, command_id: str) -> tuple[int, int] | None:
        """The sequence range a previously applied command produced, if any."""
        if not command_id:
            return None
        row = self._con.execute(
            "SELECT first_seq, last_seq FROM processed_commands WHERE command_id = ?",
            (command_id,),
        ).fetchone()
        return None if row is None else (int(row[0]), int(row[1]))

    def known_commands(self, command_ids: Sequence[str]) -> set[str]:
        """Which of these command ids the log has already applied."""
        found: set[str] = set()
        for start in range(0, len(command_ids), _IN_CHUNK):
            chunk = [cid for cid in command_ids[start : start + _IN_CHUNK] if cid]
            if not chunk:
                continue
            placeholders = ",".join("?" * len(chunk))
            cursor = self._con.execute(
                f"SELECT command_id FROM processed_commands WHERE command_id IN ({placeholders})",
                chunk,
            )
            found.update(str(row[0]) for row in cursor)
        return found

    def command_count(self) -> int:
        return int(self._con.execute("SELECT COUNT(*) FROM processed_commands").fetchone()[0])

    # -------------------------------------------------------------- integrity

    def verify(self) -> int:
        """Walk the whole chain and confirm nothing was altered.

        Every record is re-hashed from the exact payload bytes stored, and each link
        is checked against its predecessor.

        Returns:
            The number of records verified.

        Raises:
            ChainBroken: on a hash mismatch, a sequence gap, or a broken link.
        """
        expected_seq, prev, count = 1, GENESIS_HASH, 0
        cursor = self._con.execute(f"{_SELECT} ORDER BY seq")
        with histogram("store.verify_ms").time():
            try:
                for seq, type_name, prev_hash, digest, data_json in cursor:
                    if seq != expected_seq:
                        raise ChainBroken(
                            f"sequence gap: expected {expected_seq}, found {seq}",
                            expected=expected_seq,
                            found=seq,
                        )
                    if prev_hash != prev:
                        raise ChainBroken(
                            f"record {seq} does not link to record {seq - 1}", seq=seq
                        )
                    recomputed = chain_hash_text(prev_hash, seq, type_name, data_json)
                    if recomputed != digest:
                        raise ChainBroken(
                            f"record {seq} ({type_name}) fails its own hash — payload was altered",
                            seq=seq,
                            type=type_name,
                        )
                    prev = digest
                    expected_seq += 1
                    count += 1
            finally:
                cursor.close()
        return count

    # ---------------------------------------------------------------- private

    def _read_head(self) -> tuple[int, str]:
        row = self._con.execute(
            "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return 0, GENESIS_HASH
        return int(row[0]), str(row[1])

    def _stream_version_uncached(self, stream_id: str) -> int:
        row = self._con.execute(
            "SELECT MAX(version) FROM events WHERE stream_id = ?", (stream_id,)
        ).fetchone()
        return 0 if row is None or row[0] is None else int(row[0])

    def _stream_versions(self, stream_ids: Iterable[str]) -> dict[str, int]:
        """Current version of each stream this batch touches, in one query per chunk."""
        wanted = sorted(stream_ids)
        out: dict[str, int] = {}
        for start in range(0, len(wanted), _IN_CHUNK):
            chunk = wanted[start : start + _IN_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            cursor = self._con.execute(
                f"SELECT stream_id, MAX(version) FROM events WHERE stream_id IN ({placeholders})"
                " GROUP BY stream_id",
                chunk,
            )
            out.update({str(row[0]): int(row[1]) for row in cursor})
        for stream in wanted:
            out.setdefault(stream, 0)
        return out


def _to_record(row: tuple[Any, ...]) -> Recorded:
    """Build a :class:`Recorded` from a database row.

    The hash is *not* recomputed here. Rehashing on every read would triple the cost
    of a replay, and it would be checking the row against itself; proving the log
    intact is :meth:`EventStore.verify`'s job, and proving it was not rewritten
    wholesale is the seal's.
    """
    seq, type_name, prev_hash, digest, data_json = row
    try:
        data = json.loads(data_json)
    except ValueError as exc:
        raise LogCorrupt(f"record {seq} holds unreadable payload text", seq=int(seq)) from exc
    return Recorded(
        seq=int(seq),
        event=event_from_data(str(type_name), data, seq=int(seq)),
        prev_hash=str(prev_hash),
        hash=str(digest),
    )


__all__ = ["CommandGroup", "DuplicateCommand", "EventStore"]
