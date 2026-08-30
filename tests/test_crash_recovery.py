"""What survives a process dying mid-write.

The failure this replaced was specific and bad: the old file-backed log was flushed
but never fsynced, and a kill during a write left a half-written final line. Reopening
raised a raw ``json.JSONDecodeError`` from the head scan, so an interrupted append did
not lose one event — it made the entire audit record inaccessible.

The tests here kill a real child process mid-append and then assert the three things
that have to hold afterwards: the log opens, the chain verifies, and everything the
child was told had committed is still there.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from sentinel.domain.events import TransactionObserved
from sentinel.errors import ChainBroken, LogCorrupt
from sentinel.store.jsonl import export_jsonl, import_jsonl, read_jsonl
from sentinel.store.sqlite_store import EventStore

ROOT = Path(__file__).resolve().parents[1]

#: Appends one group at a time, printing the sequence number the store confirmed.
#: The parent kills it partway through, so the last printed number is the newest
#: append the writer was told had committed.
WRITER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, __ROOT__)
    from sentinel.domain.events import TransactionObserved
    from sentinel.store.sqlite_store import EventStore

    store = EventStore(__PATH__)
    for n in range(1, 100000):
        rec = store.append(
            TransactionObserved(n, "C" + str(n % 40), float(n), 10.0 + n,
                                "fuel", 1.0, 2.0, 3.0, 0, -1, "")
        )
        print(rec.seq, flush=True)
    """
)


def _observation(n: int) -> TransactionObserved:
    return TransactionObserved(n, f"C{n % 40:04d}", float(n), 10.0 + n, "fuel",
                               1.0, 2.0, 3.0, 0, -1, "")


def test_a_killed_writer_loses_nothing_it_acknowledged(tmp_path):
    path = tmp_path / "killed.db"
    script = WRITER.replace("__ROOT__", repr(str(ROOT))).replace(
        "__PATH__", repr(str(path))
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, cwd=str(tmp_path)
    )
    acknowledged = 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            acknowledged = int(line.strip())
            if acknowledged >= 200:
                break
    finally:
        proc.kill()
        proc.wait(timeout=30)

    assert acknowledged >= 200, "the writer never got far enough to be worth killing"
    with EventStore(path) as store:
        assert store.verify() == store.count()
        assert store.last_seq >= acknowledged
        seqs = [r.seq for r in store.read(upto_seq=acknowledged)]
        assert seqs == list(range(1, acknowledged + 1))


def test_a_failed_group_leaves_no_partial_records(tmp_path):
    """All-or-nothing: a group that violates a constraint writes none of itself."""
    with EventStore.create_fresh(tmp_path / "atomic.db") as store:
        store.append_many([_observation(n) for n in range(1, 6)])
        before = (store.last_seq, store.head_hash)

        # The tenth event repeats txn_id 3, which the unique index refuses. The nine
        # good events ahead of it in the same group must not survive.
        clashing = [_observation(n) for n in range(6, 15)] + [_observation(3)]
        with pytest.raises(Exception):
            store.append_many(clashing)

        assert (store.last_seq, store.head_hash) == before
        assert store.count() == 5
        assert store.verify() == 5


def test_reopening_after_a_kill_continues_the_same_chain(tmp_path):
    path = tmp_path / "resume.db"
    with EventStore.create_fresh(path) as first:
        first.append_many([_observation(n) for n in range(1, 21)])
        head = first.head_hash

    with EventStore(path) as second:
        rec = second.append(_observation(21))
        assert rec.prev_hash == head
        assert second.verify() == 21


def test_a_torn_archive_tail_is_recoverable(tmp_path, log_copy):
    """A partial final line is an interruption, not an edit — drop it and carry on."""
    archive = tmp_path / "log.jsonl"
    written = export_jsonl(log_copy, archive)
    raw = archive.read_bytes()
    torn = raw[: len(raw) - 40]
    archive.write_bytes(torn)

    with pytest.raises(LogCorrupt, match="not a complete record"):
        list(read_jsonl(archive))

    recovered = list(read_jsonl(archive, recover=True))
    assert len(recovered) == written - 1
    assert [r.seq for r in recovered] == list(range(1, written))


def test_a_recovered_archive_imports_into_the_same_chain(tmp_path, log_copy):
    archive = tmp_path / "log.jsonl"
    export_jsonl(log_copy, archive)
    raw = archive.read_bytes()
    archive.write_bytes(raw[: len(raw) - 40])

    with EventStore.create_fresh(tmp_path / "restored.db") as restored:
        report = import_jsonl(archive, restored, recover=True)
        assert report.imported == log_copy.last_seq - 1
        assert report.skipped_tail_bytes > 0
        assert restored.verify() == report.imported
        assert restored.head_hash == log_copy.hash_at(report.imported)


def test_an_edit_in_the_middle_of_an_archive_is_never_recovered(tmp_path, log_copy):
    """``recover`` forgives a torn tail and nothing else."""
    archive = tmp_path / "log.jsonl"
    export_jsonl(log_copy, archive)
    lines = archive.read_text(encoding="utf-8").splitlines()
    index = next(i for i, ln in enumerate(lines) if '"TransactionObserved"' in ln)
    lines[index] = lines[index].replace('"amount":', '"amount":1.0,"_amount":', 1)
    archive.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ChainBroken):
        list(read_jsonl(archive, recover=True))
