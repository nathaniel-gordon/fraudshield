"""The log must be append-only and provably unaltered.

These are the foundational tests: if the store can silently lose, reorder or edit a
record, every downstream guarantee — replay, time travel, audit — is worthless.

Tampering is simulated with raw SQL against the database file, which is exactly the
attacker model: somebody with write access to the artifact, not somebody going through
this package's API.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sentinel.domain.events import (
    AlertRaised,
    CaseDispositioned,
    CaseOpened,
    DetectionPolicyAdopted,
    RiskScored,
    RuleTriggered,
    TransactionObserved,
)
from sentinel.errors import ChainBroken
from sentinel.store.codec import GENESIS_HASH, canonical_json, chain_hash, decode, encode
from sentinel.store.sqlite_store import EventStore

SAMPLE_EVENTS = [
    TransactionObserved(7, "C0001", 1234.5, 99.95, "fuel", 1.5, -2.25, 13.5, 0, 1, "burst"),
    RuleTriggered(7, "C0001", "amount_spike", 0.5, "amount 99.95 is 6.1 std above card norm"),
    RiskScored(7, "C0001", 0.9123, 0.5, 0.7930, 0.2, 0.35),
    AlertRaised("ALERT-00000007", 7, "C0001", 1234.5, 99.95, 0.8722, "hybrid"),
    CaseOpened("CASE-00000007", "ALERT-00000007", 7, "C0001", 1234.5, 0.8722, 99.95, "head"),
    CaseDispositioned("CASE-00000007", "confirmed_fraud", "a.okafor", "blocked", 9999.0),
    DetectionPolicyAdopted("m1", 0.2, 0.35, 0.85, 12.0, 450.0, 0.99, 0.98, 100, ("amount",)),
]


@pytest.mark.parametrize("event", SAMPLE_EVENTS, ids=lambda e: type(e).__name__)
def test_codec_round_trips_every_field(event):
    """Serialise -> parse must return an equal event, not merely a similar one."""
    line, recorded = encode(4, event, GENESIS_HASH)
    parsed = decode(line)
    assert parsed.seq == 4
    assert parsed.hash == recorded.hash
    assert parsed.event == event


def test_codec_restores_tuple_fields_flattened_by_json():
    """``features`` is declared a tuple; JSON only knows lists."""
    line, _ = encode(1, SAMPLE_EVENTS[-1], GENESIS_HASH)
    parsed = decode(line)
    assert parsed.event.features == ("amount",)


def test_append_assigns_contiguous_sequence_and_chains(tmp_path):
    with EventStore.create_fresh(tmp_path / "log.db") as store:
        records = store.append_many(SAMPLE_EVENTS[:3])
        assert [r.seq for r in records] == [1, 2, 3]
        assert records[0].prev_hash == GENESIS_HASH
        assert records[1].prev_hash == records[0].hash
        assert records[2].prev_hash == records[1].hash
        assert store.last_seq == 3 and store.head_hash == records[2].hash


def test_reopening_resumes_the_chain(tmp_path):
    """A second process must continue the log, not restart it."""
    path = tmp_path / "log.db"
    with EventStore.create_fresh(path) as first:
        first.append_many(SAMPLE_EVENTS[:3])
        head, last = first.head_hash, first.last_seq

    with EventStore(path) as second:
        assert (second.last_seq, second.head_hash) == (last, head)
        rec = second.append(SAMPLE_EVENTS[3])
        assert rec.seq == 4 and rec.prev_hash == head
        assert second.verify() == 4


def test_events_are_streamed_per_card(tmp_path):
    """Every fact about one card lands on that card's stream, versioned in order."""
    with EventStore.create_fresh(tmp_path / "log.db") as store:
        store.append_many(SAMPLE_EVENTS[:4])
        assert store.streams("card:") == ["card:C0001"]
        assert store.stream_version("card:C0001") == 4
        assert store.stream_version("card:NOPE") == 0


def test_verify_walks_the_whole_session_log(store):
    assert store.verify() == store.last_seq
    assert store.count() == store.last_seq
    assert store.last_seq > 20_000, "the session corpus should produce a substantial log"


def test_read_upto_seq_returns_an_exact_prefix(store):
    prefix = list(store.read(upto_seq=500))
    assert [r.seq for r in prefix] == list(range(1, 501))
    full_first_500 = [r.hash for r in store.read(limit=500)]
    assert [r.hash for r in prefix] == full_first_500


def test_read_can_filter_by_type(store):
    counts = store.type_counts()
    scored = list(store.read(types=["RiskScored"], limit=25))
    assert len(scored) == 25
    assert {r.type for r in scored} == {"RiskScored"}
    assert counts["RiskScored"] == counts["TransactionObserved"]


def test_editing_a_payload_is_detected(log_copy):
    """The classic attack: quietly reduce an amount so a case looks harmless."""
    con = log_copy.connection
    seq, data = con.execute(
        "SELECT seq, data FROM events WHERE type = 'TransactionObserved' ORDER BY seq LIMIT 1"
    ).fetchone()
    edited = canonical_json({**json.loads(data), "amount": 1.0})
    con.execute("UPDATE events SET data = ? WHERE seq = ?", (edited, seq))
    with pytest.raises(ChainBroken, match="fails its own hash"):
        log_copy.verify()


def test_deleting_a_record_is_detected(log_copy):
    """Removing an inconvenient event breaks the sequence, not just the hash."""
    log_copy.connection.execute("DELETE FROM events WHERE seq = 400")
    with pytest.raises(ChainBroken, match="sequence gap"):
        log_copy.verify()


def test_reordering_records_is_detected(log_copy):
    """Two records swapped: each still hashes correctly, but the links do not."""
    con = log_copy.connection
    rows = con.execute("SELECT * FROM events WHERE seq IN (300, 301) ORDER BY seq").fetchall()
    columns = [d[0] for d in con.execute("SELECT * FROM events LIMIT 1").description]
    con.execute("DELETE FROM events WHERE seq IN (300, 301)")
    placeholders = ",".join("?" * len(columns))
    for seq, row in zip((301, 300), rows):
        values = list(row)
        values[columns.index("seq")] = seq
        con.execute(f"INSERT INTO events ({','.join(columns)}) VALUES ({placeholders})", values)
    with pytest.raises(ChainBroken, match="does not link"):
        log_copy.verify()


def test_a_fully_recomputed_chain_still_verifies(log_copy):
    """The honest limit of a bare hash chain, asserted rather than assumed.

    An attacker with write access can edit a payload and recompute every downstream
    hash; ``verify()`` then passes. This is exactly why the seal exists, and why no
    docstring in this package may claim the chain alone proves nothing was edited.
    """
    con = log_copy.connection
    seq, data = con.execute(
        "SELECT seq, data FROM events WHERE type = 'TransactionObserved' ORDER BY seq LIMIT 1"
    ).fetchone()
    edited = canonical_json({**json.loads(data), "amount": 1.0})
    con.execute("UPDATE events SET data = ? WHERE seq = ?", (edited, seq))
    _recompute_chain_from(con, seq)
    assert log_copy.verify() == log_copy.count()


def _recompute_chain_from(con: sqlite3.Connection, seq: int) -> None:
    """Rewrite every hash from ``seq`` forward so the chain is internally consistent."""
    prev = con.execute("SELECT prev_hash FROM events WHERE seq = ?", (seq,)).fetchone()[0]
    rows = con.execute(
        "SELECT seq, type, data FROM events WHERE seq >= ? ORDER BY seq", (seq,)
    ).fetchall()
    updates = []
    for row_seq, type_name, data in rows:
        digest = chain_hash(prev, row_seq, type_name, json.loads(data))
        updates.append((prev, digest, row_seq))
        prev = digest
    con.execute("BEGIN IMMEDIATE")
    con.executemany("UPDATE events SET prev_hash = ?, hash = ? WHERE seq = ?", updates)
    con.commit()


def test_store_has_no_mutating_sql_outside_the_named_destructor(tmp_path):
    """Append-only is a property of the code, not of a method-name blocklist.

    The old version of this test asserted a set of verb names was absent from
    ``dir(EventStore)``, which was trivially true and missed that the destructive path
    was the constructor. This one reads the module: the only statements that can
    remove or overwrite an event row must not exist.
    """
    import sentinel.store.sqlite_store as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    assert "update events" not in lowered
    assert "delete from events" not in lowered
    assert "drop table" not in lowered

    with EventStore.create_fresh(tmp_path / "log.db") as store:
        store.append_many(SAMPLE_EVENTS[:2])
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO events (seq, stream_id, version, type, prev_hash, hash, data)"
                " VALUES (1, 'card:X', 1, 'T', ?, ?, '{}')",
                (GENESIS_HASH, "f" * 64),
            )
