"""Every rejection branch on the write side, and what it costs to skip one.

The interesting case is NaN: ``json.dumps`` writes it as the bare literal ``NaN``,
which is not valid JSON. One unvalidated NaN amount and the log stops being readable
by anything that is not Python — including the archival JSONL the whole evidence story
rests on. The rest are ordinary range checks, but they are the difference between a
corrupt feed being refused at the door and it being folded into every per-card average
for ever.
"""
from __future__ import annotations

from dataclasses import fields

import pytest

from sentinel.commands.handlers import (
    MAX_AMOUNT,
    AdoptDetectionPolicyHandler,
    DispositionCaseHandler,
    ObserveTransactionHandler,
)
from sentinel.commands.messages import AdoptDetectionPolicy, DispositionCase, ObserveTransaction
from sentinel.detection.risk_engine import RiskEngine
from sentinel.domain.clock import SteppingClock
from sentinel.domain.events import Disposition
from sentinel.errors import (
    CaseAlreadyClosed,
    CommandRejected,
    OutOfOrder,
    UnknownCase,
    ValidationFailed,
)

GOOD = ObserveTransaction(
    txn_id=1, card_id="C0001", occurred_at=100.0, amount=42.0, category="fuel",
    lat=51.5, lon=-0.12, hour=9.0, is_online=0,
)

POLICY = AdoptDetectionPolicy(
    model_id="m1", alert_threshold=0.2, rule_weight=0.35, rules_only_floor=0.85,
    fp_cost=12.0, fn_cost=450.0, auc=0.9, avg_precision=0.8, train_rows=10,
    features=("amount",),
)


def replace(command, **changes):
    """``dataclasses.replace`` equivalent that also works on slotted frozen commands."""
    values = {f.name: getattr(command, f.name) for f in fields(command)}
    return type(command)(**{**values, **changes})


@pytest.fixture
def observe(built):
    """A fresh observation handler over the session's fitted detector."""
    models = built.bus.handler_for(ObserveTransaction).risk.models
    return ObserveTransactionHandler(RiskEngine(models), built.policy)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"card_id": ""}, "missing card_id"),
        ({"card_id": "   "}, "missing card_id"),
        ({"txn_id": -3}, "non-negative"),
        ({"amount": -0.01}, "negative amount"),
        ({"amount": MAX_AMOUNT + 1}, "ceiling"),
        ({"amount": float("nan")}, "finite"),
        ({"amount": float("inf")}, "finite"),
        ({"occurred_at": float("nan")}, "finite"),
        ({"lat": 91.0}, "off the globe"),
        ({"lat": -90.5}, "off the globe"),
        ({"lon": 180.5}, "off the globe"),
        ({"hour": 24.0}, "time of day"),
        ({"hour": -1.0}, "time of day"),
        ({"is_online": 2}, "0 or 1"),
    ],
)
def test_bad_observations_are_refused(observe, changes, match):
    with pytest.raises(ValidationFailed, match=match):
        observe.handle(replace(GOOD, **changes))


def test_a_nan_amount_would_have_produced_unreadable_json():
    """Why the finiteness check is not merely tidy."""
    import json

    assert json.dumps({"amount": float("nan")}) == '{"amount": NaN}'
    with pytest.raises(ValueError):
        json.loads('{"amount": NaN}', parse_constant=_reject)


def _reject(literal):
    raise ValueError(f"{literal} is not JSON")


def test_observations_must_not_go_backwards(observe):
    """The feature stream is online; a late arrival would corrupt every window."""
    observe.handle(GOOD)
    observe.handle(replace(GOOD, txn_id=2, occurred_at=200.0))
    with pytest.raises(OutOfOrder, match="before the watermark"):
        observe.handle(replace(GOOD, txn_id=3, occurred_at=150.0))


def test_a_repeated_timestamp_is_allowed(observe):
    """Two transactions in the same second are ordinary, not out of order."""
    observe.handle(GOOD)
    events = observe.handle(replace(GOOD, txn_id=2))
    assert events[0].txn_id == 2


def test_rejection_emits_no_events(observe):
    """A refusal is a non-event; the handler must return nothing at all."""
    with pytest.raises(CommandRejected):
        observe.handle(replace(GOOD, amount=-1.0))
    assert observe.handle(GOOD)[0].type == "TransactionObserved"


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"rule_weight": 1.5}, "rule_weight"),
        ({"rule_weight": -0.1}, "rule_weight"),
        ({"alert_threshold": 0.0}, "alert_threshold"),
        ({"alert_threshold": 1.5}, "alert_threshold"),
        ({"alert_threshold": float("nan")}, "finite"),
        ({"rules_only_floor": 1.2}, "rules_only_floor"),
        ({"model_id": " "}, "model it adopts"),
        ({"features": ()}, "features"),
    ],
)
def test_bad_policies_are_refused(changes, match):
    with pytest.raises(ValidationFailed, match=match):
        AdoptDetectionPolicyHandler().handle(replace(POLICY, **changes))


def test_a_good_policy_becomes_the_log_header():
    events = AdoptDetectionPolicyHandler().handle(POLICY)
    assert [e.type for e in events] == ["DetectionPolicyAdopted"]
    assert events[0].features == ("amount",)


def test_dispositioning_an_unknown_case_is_refused(built):
    handler = DispositionCaseHandler(built.read_model.cases, SteppingClock(0.0))
    with pytest.raises(UnknownCase, match="unknown case"):
        handler.handle(DispositionCase("CASE-99999999", Disposition.GENUINE, "a.okafor"))


def test_a_disposition_must_name_an_analyst(built):
    handler = DispositionCaseHandler(built.read_model.cases, SteppingClock(0.0))
    case_id = built.read_model.cases.open_cases(1)[0].case_id
    with pytest.raises(ValidationFailed, match="analyst"):
        handler.handle(DispositionCase(case_id, Disposition.GENUINE, "  "))


def test_an_unknown_disposition_string_is_refused(built):
    handler = DispositionCaseHandler(built.read_model.cases, SteppingClock(0.0))
    case_id = built.read_model.cases.open_cases(1)[0].case_id
    with pytest.raises(ValidationFailed, match="not a disposition"):
        handler.handle(DispositionCase(case_id, "probably_fine", "a.okafor"))  # type: ignore[arg-type]


def test_a_closed_case_cannot_be_closed_again(log_copy, built):
    from sentinel.ingest import wire
    from sentinel.projections.registry import ReadModel

    read_model = ReadModel.rebuild(log_copy)
    models = built.bus.handler_for(ObserveTransaction).risk.models
    bus, _ = wire(log_copy, models, built.policy, read_model)
    case_id = read_model.cases.open_cases(1)[0].case_id

    bus.submit(DispositionCase(case_id, Disposition.GENUINE, "a.okafor"))
    with pytest.raises(CaseAlreadyClosed, match="already dispositioned"):
        bus.submit(DispositionCase(case_id, Disposition.CONFIRMED_FRAUD, "b.singh"))


def test_every_rejection_carries_a_stable_code(observe):
    with pytest.raises(ValidationFailed) as caught:
        observe.handle(replace(GOOD, amount=-1.0))
    payload = caught.value.as_dict()
    assert payload["error"] == "validation_failed"
    assert payload["details"]["field"] == "amount"
