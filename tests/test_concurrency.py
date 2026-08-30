"""Two writers on one log.

Before the store was a database, nothing stopped a ``build`` and a ``dashboard``
session from holding handles on the same file: both scanned the head, both decided the
next sequence number was the same one, and the log ended up with duplicate sequence
numbers that only ``verify()`` noticed, long after the writes had landed.

The database makes that structurally impossible in two independent ways — the write
lock taken by ``BEGIN IMMEDIATE`` and the ``UNIQUE(stream_id, version)`` index — and
the optimistic-concurrency check gives a caller a way to say which version it believed
it was extending.
"""
from __future__ import annotations

import threading

import pytest

from sentinel.domain.events import CaseDispositioned, TransactionObserved
from sentinel.errors import ConcurrencyConflict
from sentinel.store.sqlite_store import EventStore


def _observation(n: int, card: str = "C0001") -> TransactionObserved:
    return TransactionObserved(n, card, float(n), 10.0 + n, "fuel", 1.0, 2.0, 3.0, 0, -1, "")


def test_two_stores_on_one_path_produce_one_contiguous_chain(tmp_path):
    path = tmp_path / "shared.db"
    with EventStore.create_fresh(path) as first, EventStore(path) as second:
        first.append(_observation(1))
        second.append(_observation(2))
        first.append(_observation(3))
        second.append(_observation(4))

    with EventStore(path) as reader:
        assert [r.seq for r in reader.read()] == [1, 2, 3, 4]
        assert [r.event.txn_id for r in reader.read()] == [1, 2, 3, 4]
        assert reader.verify() == 4


def test_racing_threads_never_reuse_a_sequence_number(tmp_path):
    """Eight writers, their own connections, one log."""
    path = tmp_path / "raced.db"
    EventStore.create_fresh(path).close()
    start = threading.Barrier(8)
    errors: list[BaseException] = []

    def writer(worker: int) -> None:
        store = EventStore(path)
        try:
            start.wait(timeout=30)
            for n in range(10):
                store.append(_observation(worker * 100 + n, card=f"C{worker:04d}"))
        except BaseException as exc:  # noqa: BLE001 - reported through the list
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, errors
    with EventStore(path) as reader:
        assert reader.verify() == 80
        assert [r.seq for r in reader.read()] == list(range(1, 81))
        assert len({r.event.txn_id for r in reader.read()}) == 80


def test_an_expected_version_that_has_moved_is_refused(tmp_path):
    """Optimistic concurrency: the loser is told, rather than overwriting."""
    path = tmp_path / "optimistic.db"
    with EventStore.create_fresh(path) as store:
        store.append_many([_observation(1), _observation(2)])
        assert store.stream_version("card:C0001") == 2

        with pytest.raises(ConcurrencyConflict, match="expected 1"):
            store.append_many([_observation(3)], expected_versions={"card:C0001": 1})

        assert store.last_seq == 2, "the refused append must write nothing"
        assert store.verify() == 2

        store.append_many([_observation(3)], expected_versions={"card:C0001": 2})
        assert store.stream_version("card:C0001") == 3


def test_streams_are_independent_units_of_concurrency(tmp_path):
    """A writer on one card is not blocked by another card's version moving."""
    with EventStore.create_fresh(tmp_path / "streams.db") as store:
        store.append_many([_observation(1, "C0001"), _observation(2, "C0002")])
        store.append_many(
            [_observation(3, "C0002")], expected_versions={"card:C0002": 1}
        )
        assert store.stream_version("card:C0001") == 1
        assert store.stream_version("card:C0002") == 2
        assert sorted(store.streams("card:")) == ["card:C0001", "card:C0002"]


def test_dispositions_stream_under_their_case(tmp_path):
    """A fact that names only a case has nowhere else to go."""
    with EventStore.create_fresh(tmp_path / "cases.db") as store:
        store.append(CaseDispositioned("CASE-00000009", "genuine", "a.okafor", "", 12.0))
        assert store.streams() == ["case:CASE-00000009"]
