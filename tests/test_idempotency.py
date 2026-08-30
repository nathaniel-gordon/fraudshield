"""A command delivered twice must not become two sets of facts.

Alert and case identities are derived from ``txn_id``, so a duplicated observation
does not merely add noise — the second ``CaseOpened`` overwrites the first in the
queue projection, and the audit trail for that case shows two of everything. This is
the property the ``processed_commands`` table and the ``ux_events_txn_observed`` index
exist to guarantee.
"""
from __future__ import annotations

import pytest

from sentinel.commands.identity import command_key
from sentinel.commands.messages import DispositionCase, ObserveTransaction
from sentinel.domain.events import Disposition
from sentinel.errors import ConcurrencyConflict
from sentinel.store.sqlite_store import EventStore

OBSERVATION = ObserveTransaction(
    txn_id=4242, card_id="C0007", occurred_at=1_000.0, amount=880.0, category="electronics",
    lat=51.5, lon=-0.12, hour=3.5, is_online=1, label=1, label_mode="takeover",
)


def test_command_key_is_derived_from_content():
    """Two identical commands are the same command; a changed field is a new one."""
    same = ObserveTransaction(**vars_of(OBSERVATION))
    assert command_key(OBSERVATION) == command_key(same)
    louder = ObserveTransaction(**{**vars_of(OBSERVATION), "amount": 881.0})
    assert command_key(OBSERVATION) != command_key(louder)


def test_an_explicit_command_id_wins_over_the_digest():
    """An upstream delivery id, when there is one, is the authority."""
    tagged = ObserveTransaction(**{**vars_of(OBSERVATION), "command_id": "delivery-9"})
    assert command_key(tagged) == "delivery-9"


def test_resubmitting_an_observation_appends_nothing(fresh_bus):
    bus, store = fresh_bus
    first = bus.submit(OBSERVATION)
    head_after_first = store.head_hash
    second = bus.submit(OBSERVATION)

    assert [r.seq for r in first] == [r.seq for r in second]
    assert [r.hash for r in first] == [r.hash for r in second]
    assert store.head_hash == head_after_first
    assert bus.duplicates == 1
    assert store.verify() == store.last_seq


def test_a_redelivery_leaves_exactly_one_observation(fresh_bus):
    bus, store = fresh_bus
    bus.submit(OBSERVATION)
    bus.submit(OBSERVATION)
    observed = list(store.read(types=["TransactionObserved"]))
    assert [r.event.txn_id for r in observed] == [OBSERVATION.txn_id]
    assert store.command_count() == 1


def test_bulk_ingest_skips_commands_already_on_the_log(fresh_bus):
    bus, store = fresh_bus
    batch = [
        ObserveTransaction(**{**vars_of(OBSERVATION), "txn_id": n, "occurred_at": 1000.0 + n})
        for n in range(1, 21)
    ]
    first = bus.submit_many(batch, batch=8)
    assert first.accepted == 20 and first.duplicates == 0
    seq_after_first = store.last_seq

    replayed = bus.submit_many(batch, batch=8)
    assert replayed.accepted == 0 and replayed.duplicates == 20 and replayed.records == 0
    assert store.last_seq == seq_after_first

    half = bus.submit_many(
        batch[10:] + [ObserveTransaction(**{**vars_of(OBSERVATION), "txn_id": 99,
                                            "occurred_at": 2000.0})],
        batch=8,
    )
    assert (half.accepted, half.duplicates) == (1, 10)
    assert store.verify() == store.last_seq


def test_a_second_transaction_under_a_used_txn_id_is_refused(fresh_bus):
    """Different content, same txn_id: the derived case identity would collide."""
    bus, store = fresh_bus
    bus.submit(OBSERVATION)
    clashing = ObserveTransaction(**{**vars_of(OBSERVATION), "amount": 12.0,
                                     "occurred_at": 1001.0})
    with pytest.raises(ConcurrencyConflict, match="txn_id"):
        bus.submit(clashing)
    assert store.count() == store.last_seq
    assert store.verify() == store.last_seq


def test_dispositioning_the_same_case_twice_is_one_fact(log_copy, built):
    """Redelivered analyst decisions are common; the second must be a no-op."""
    from sentinel.commands.messages import ObserveTransaction as _Observe
    from sentinel.ingest import wire
    from sentinel.projections.registry import ReadModel

    read_model = ReadModel.rebuild(log_copy)
    models = built.bus.handler_for(_Observe).risk.models
    bus, _ = wire(log_copy, models, built.policy, read_model)
    case_id = read_model.cases.open_cases(1)[0].case_id

    close = DispositionCase(case_id, Disposition.CONFIRMED_FRAUD, "a.okafor", "card hot")
    records = bus.submit(close)
    after_first = log_copy.last_seq
    again = bus.submit(close)

    assert log_copy.last_seq == after_first
    assert [r.seq for r in records] == [r.seq for r in again]
    assert read_model.cases.get(case_id).disposition == Disposition.CONFIRMED_FRAUD.value
    assert log_copy.verify() == log_copy.last_seq


def vars_of(command) -> dict:
    """Field values of a slotted frozen dataclass, as a plain dict."""
    from dataclasses import fields

    return {f.name: getattr(command, f.name) for f in fields(command)}


@pytest.fixture
def fresh_bus(tmp_path, built):
    """An empty log wired to its own bus, reusing the session's fitted detector.

    Reusing the detector keeps the fixture cheap; the assertions here are about the
    write path, not about scores.
    """
    from sentinel.commands.messages import ObserveTransaction as _Observe
    from sentinel.detection.risk_engine import RiskEngine
    from sentinel.ingest import wire

    models = built.bus.handler_for(_Observe).risk.models
    store = EventStore.create_fresh(tmp_path / "fresh.db")
    bus, _ = wire(store, models, built.policy)
    # A private feature stream, so this log's scores do not depend on the session log.
    bus.handler_for(_Observe).risk = RiskEngine(models)
    try:
        yield bus, store
    finally:
        store.close()
