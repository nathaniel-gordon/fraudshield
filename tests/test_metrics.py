"""The metrics registry, and the fact that the write path actually feeds it.

Two halves. The first tests the instruments themselves — counting, bucketing,
snapshot shape, thread safety — against a private registry so nothing here depends
on what other tests did to the process-wide one. The second half asserts the
instrumented code paths emit: an append moves ``store.events_appended``, a rebuild
records a hit or a miss, and ``--metrics`` on the CLI prints one JSON document on
stderr that carries all of it.
"""
from __future__ import annotations

import json
import threading

import pytest

from sentinel import metrics
from sentinel.metrics import Counter, Histogram, Registry

# --------------------------------------------------------------- instruments


def test_a_counter_counts_and_refuses_to_go_down():
    c = Counter()
    c.inc()
    c.inc(41)
    assert c.value == 42
    with pytest.raises(ValueError):
        c.inc(-1)


def test_histogram_buckets_are_cumulative_and_the_overflow_is_counted():
    h = Histogram(buckets=(1, 10, 100))
    for ms in (0.4, 0.6, 5, 50, 5000):
        h.observe(ms)
    snap = h.snapshot()
    assert snap["count"] == 5
    assert snap["buckets_ms"] == {"1": 2, "10": 3, "100": 4, "+Inf": 5}
    assert snap["min_ms"] == 0.4
    assert snap["max_ms"] == 5000
    assert snap["sum_ms"] == pytest.approx(5056.0)


def test_an_observation_on_a_bucket_bound_lands_inside_it():
    h = Histogram(buckets=(1, 10))
    h.observe(10)
    assert h.snapshot()["buckets_ms"]["10"] == 1


def test_unsorted_or_duplicated_buckets_are_refused():
    with pytest.raises(ValueError):
        Histogram(buckets=(10, 1))
    with pytest.raises(ValueError):
        Histogram(buckets=(1, 1, 10))


def test_the_timer_records_even_when_the_block_raises():
    h = Histogram()
    with pytest.raises(RuntimeError), h.time():
        raise RuntimeError("boom")
    assert h.snapshot()["count"] == 1


def test_a_name_is_one_kind_of_instrument_only():
    reg = Registry()
    reg.counter("x")
    with pytest.raises(ValueError):
        reg.histogram("x")
    reg.histogram("y")
    with pytest.raises(ValueError):
        reg.counter("y")


def test_asking_twice_returns_the_same_instrument():
    reg = Registry()
    assert reg.counter("a") is reg.counter("a")
    assert reg.histogram("b") is reg.histogram("b")


def test_the_snapshot_is_plain_json_data():
    reg = Registry()
    reg.counter("events").inc(3)
    reg.histogram("lat").observe(12.5)
    doc = json.loads(json.dumps(reg.snapshot()))
    assert doc["counters"] == {"events": 3}
    assert doc["histograms"]["lat"]["count"] == 1


def test_concurrent_increments_are_not_lost():
    reg = Registry()
    c = reg.counter("hits")
    h = reg.histogram("lat")

    def hammer():
        for _ in range(1000):
            c.inc()
            h.observe(1.0)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.value == 8000
    assert h.snapshot()["count"] == 8000


# ------------------------------------------------------ instrumented paths


def test_an_append_feeds_the_store_counters(tmp_path):
    from sentinel.domain.events import TransactionObserved
    from sentinel.store.sqlite_store import EventStore

    observed = TransactionObserved(7, "C0001", 1234.5, 99.95, "fuel", 1.5, -2.25, 13.5,
                                   0, 1, "burst")
    before = metrics.snapshot()["counters"].get("store.events_appended", 0)
    store = EventStore.create_fresh(tmp_path / "m.db")
    try:
        store.append(observed)
        store.verify()
    finally:
        store.close()
    after = metrics.snapshot()
    assert after["counters"]["store.events_appended"] == before + 1
    assert after["histograms"]["store.append_ms"]["count"] >= 1
    assert after["histograms"]["store.verify_ms"]["count"] >= 1


def test_a_rebuild_records_a_checkpoint_hit_or_miss(log_copy):
    from sentinel.projections.checkpoints import CheckpointStore, rebuild_with_checkpoints
    from sentinel.projections.registry import ReadModel

    def count(name):
        return metrics.snapshot()["counters"].get(name, 0)

    misses = count("rebuild.checkpoint_miss")
    rebuild_with_checkpoints(log_copy)  # the copy holds no checkpoint yet
    assert count("rebuild.checkpoint_miss") == misses + 1

    CheckpointStore(log_copy).save(ReadModel.rebuild(log_copy))
    hits = count("rebuild.checkpoint_hit")
    rebuild_with_checkpoints(log_copy)
    assert count("rebuild.checkpoint_hit") == hits + 1
    assert metrics.snapshot()["histograms"]["rebuild.replay_ms"]["count"] >= 2


def test_the_cli_flag_dumps_one_json_document_on_stderr(tmp_path, capsys):
    from sentinel.__main__ import main

    path = tmp_path / "cli.db"
    assert main(["--log", str(path), "--metrics", "build", "--cards", "20", "--days", "4"]) == 0
    err_lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    doc = json.loads(err_lines[-1])
    assert doc["command"] == "build"
    assert doc["counters"]["store.events_appended"] > 0
    assert doc["counters"]["bus.commands_accepted"] > 0
    assert doc["histograms"]["store.append_ms"]["count"] > 0
    assert "buckets_ms" in doc["histograms"]["bus.ingest_ms"]
