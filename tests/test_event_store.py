"""The log must be append-only and provably unaltered.

These are the foundational tests: if the store can silently lose, reorder or edit a
record, every downstream guarantee — replay, time travel, audit — is worthless.
"""
from __future__ import annotations

import json

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
from sentinel.store.codec import GENESIS_HASH, ChainBroken, decode, encode
from sentinel.store.jsonl_store import EventStore

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


def test_append_assigns_contiguous_sequence_and_chains(tmp_path):
    store = EventStore(tmp_path / "log.jsonl", append=False)
    records = store.append_many(SAMPLE_EVENTS[:3])
    assert [r.seq for r in records] == [1, 2, 3]
    assert records[0].prev_hash == GENESIS_HASH
    assert records[1].prev_hash == records[0].hash
    assert records[2].prev_hash == records[1].hash
    assert store.last_seq == 3 and store.head_hash == records[2].hash
    store.close()


def test_reopening_resumes_the_chain(tmp_path):
    """A second process must continue the log, not restart it."""
    path = tmp_path / "log.jsonl"
    first = EventStore(path, append=False)
    first.append_many(SAMPLE_EVENTS[:3])
    head, last = first.head_hash, first.last_seq
    first.close()

    second = EventStore(path)
    assert (second.last_seq, second.head_hash) == (last, head)
    rec = second.append(SAMPLE_EVENTS[3])
    assert rec.seq == 4 and rec.prev_hash == head
    assert second.verify() == 4
    second.close()


def test_verify_walks_the_whole_session_log(store):
    assert store.verify() == store.last_seq
    assert store.last_seq > 20_000, "the session corpus should produce a substantial log"


def test_read_upto_seq_returns_an_exact_prefix(store):
    prefix = list(store.read(upto_seq=500))
    assert [r.seq for r in prefix] == list(range(1, 501))
    full_first_500 = [r.hash for r in store.read()][:500]
    assert [r.hash for r in prefix] == full_first_500


def test_editing_a_payload_is_detected(tmp_path, store):
    """The classic attack: quietly reduce an amount so a case looks harmless."""
    target = tmp_path / "tampered.jsonl"
    lines = store.path.read_text(encoding="utf-8").splitlines()
    index = next(i for i, ln in enumerate(lines) if '"type":"TransactionObserved"' in ln)
    record = json.loads(lines[index])
    record["data"]["amount"] = 1.0
    lines[index] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ChainBroken, match="fails its own hash"):
        EventStore(target).verify()


def test_deleting_a_record_is_detected(tmp_path, store):
    """Removing an inconvenient event breaks the sequence, not just the hash."""
    target = tmp_path / "gapped.jsonl"
    lines = store.path.read_text(encoding="utf-8").splitlines()
    del lines[400]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ChainBroken, match="sequence gap"):
        EventStore(target).verify()


def test_reordering_records_is_detected(tmp_path, store):
    """Two records swapped: each still hashes correctly, but the links do not."""
    target = tmp_path / "swapped.jsonl"
    lines = store.path.read_text(encoding="utf-8").splitlines()
    lines[300], lines[301] = lines[301], lines[300]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ChainBroken):
        EventStore(target).verify()


def test_store_exposes_no_mutation_verbs():
    """The architecture's guarantee is enforced by the absence of these methods."""
    forbidden = {"update", "delete", "remove", "truncate", "set", "replace", "edit"}
    assert forbidden.isdisjoint(dir(EventStore))
