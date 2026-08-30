"""The chain alone does not prove nobody edited the log; the seal does.

``test_event_store.py`` asserts the uncomfortable half of this: an attacker with write
access can change a payload, recompute every downstream hash, and ``verify()`` returns
clean. The seal closes it by MACing the head with a key the writer does not hold.

These tests are the reason the codec's docstring may say what it says.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from sentinel.errors import SealMismatch
from sentinel.store.codec import canonical_json, chain_hash
from sentinel.store.seal import ENV_KEY, key_id, load_key, seal, seals, verify_seals

KEY = b"a-key-that-lives-outside-the-log"
OTHER = b"somebody-elses-key"


def test_sealing_records_the_head_and_verifies(log_copy):
    entry = seal(log_copy, KEY)
    assert entry.seq == log_copy.last_seq
    assert entry.head_hash == log_copy.head_hash
    assert entry.key_id == key_id(KEY)

    check = verify_seals(log_copy, KEY)
    assert check.ok and check.checked == 1


def test_resealing_the_same_head_is_idempotent(log_copy):
    first = seal(log_copy, KEY, now=1000.0)
    second = seal(log_copy, KEY, now=2000.0)
    assert first.mac == second.mac
    assert len(seals(log_copy)) == 1


def test_a_rewritten_history_fails_its_seal(log_copy):
    """The attack the bare chain cannot see."""
    seal(log_copy, KEY)
    con = log_copy.connection
    seq, data = con.execute(
        "SELECT seq, data FROM events WHERE type = 'TransactionObserved' ORDER BY seq LIMIT 1"
    ).fetchone()
    con.execute(
        "UPDATE events SET data = ? WHERE seq = ?",
        (canonical_json({**json.loads(data), "amount": 1.0}), seq),
    )
    _recompute_chain_from(con, seq)

    assert log_copy.verify() == log_copy.count(), "the chain itself still looks fine"
    with pytest.raises(SealMismatch, match="history was rewritten"):
        verify_seals(log_copy, KEY)


def test_a_seal_written_with_another_key_is_reported(log_copy):
    seal(log_copy, OTHER)
    with pytest.raises(SealMismatch, match="was written with key"):
        verify_seals(log_copy, KEY)


def test_dropping_the_sealed_record_is_detected(log_copy):
    entry = seal(log_copy, KEY)
    log_copy.connection.execute("DELETE FROM events WHERE seq = ?", (entry.seq,))
    with pytest.raises(SealMismatch, match="no longer holds"):
        verify_seals(log_copy, KEY)


def test_an_unsealed_log_says_so_and_can_be_made_to_fail(log_copy):
    check = verify_seals(log_copy, KEY)
    assert check.ok and check.checked == 0 and "no seal" in check.detail
    with pytest.raises(SealMismatch, match="carries no seal"):
        verify_seals(log_copy, KEY, require=True)


def test_appending_after_a_seal_leaves_the_seal_valid(log_copy):
    from sentinel.domain.events import CaseDispositioned

    first = seal(log_copy, KEY)
    log_copy.append(CaseDispositioned("CASE-00000001", "genuine", "a.okafor", "", 1.0))
    assert verify_seals(log_copy, KEY).ok

    second = seal(log_copy, KEY)
    assert second.seq > first.seq
    assert len(seals(log_copy)) == 2
    assert verify_seals(log_copy, KEY).checked == 2


def test_refusing_to_seal_an_empty_log(tmp_path):
    from sentinel.store.sqlite_store import EventStore

    with EventStore.create_fresh(tmp_path / "empty.db") as store:
        with pytest.raises(SealMismatch, match="empty log"):
            seal(store, KEY)


def test_the_key_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)
    with pytest.raises(SealMismatch, match=ENV_KEY):
        load_key()
    monkeypatch.setenv(ENV_KEY, "from-the-environment")
    assert load_key() == b"from-the-environment"
    assert load_key("explicit") == b"explicit"


def test_the_key_id_does_not_leak_the_key():
    identifier = key_id(KEY)
    assert len(identifier) == 12
    assert KEY.decode() not in identifier
    assert key_id(KEY) != key_id(OTHER)


def _recompute_chain_from(con: sqlite3.Connection, seq: int) -> None:
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
