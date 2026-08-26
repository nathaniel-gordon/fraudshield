"""Immutable domain events — the only source of truth in the sentinel.

Why events instead of tables: a card-fraud platform has to answer, months after the
fact, "why was this card alerted, and what did we actually know at that moment?".
Mutable rows cannot answer that — an UPDATE destroys the evidence. An append-only
log of facts can, and it also lets us re-derive every read model, replay the log
under a different policy, and show an analyst the exact ordered facts behind a case.

Every event class here is a *frozen, slotted* dataclass. Once a fact is recorded no
command handler, projection or UI widget can retroactively edit history; the only
legal way to change the world is to append a new fact.

Event vocabulary (chronological within one transaction):

    DetectionPolicyAdopted   once, at the head of the log — makes the log self-describing
    TransactionObserved      a transaction arrived
    RuleTriggered            an explainable rule fired on it (zero or more)
    RiskScored               the ensemble + rules produced a score under a stated policy
    AlertRaised              the policy decided the score warranted an alert
    CaseOpened               the alert became a queue item for a human
    CaseDispositioned        an analyst closed the case
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, fields
from typing import Any


class Disposition(str, enum.Enum):
    """Terminal outcomes an analyst can record against a case.

    Subclasses ``str`` so the value serialises to plain JSON without a custom
    encoder — event payloads must stay human-readable in the log file.
    """

    CONFIRMED_FRAUD = "confirmed_fraud"
    GENUINE = "genuine"
    ESCALATED = "escalated"

    @classmethod
    def parse(cls, raw: str | "Disposition") -> "Disposition":
        if isinstance(raw, cls):
            return raw
        return cls(str(raw))


class DomainEvent:
    """Base marker for recorded facts.

    Deliberately *not* a dataclass itself: a plain slotted base keeps subclass
    ``@dataclass(frozen=True, slots=True)`` decoration simple and avoids the
    frozen/non-frozen inheritance rules.
    """

    __slots__ = ()

    @property
    def type(self) -> str:
        """Wire type name; the codec uses this to route deserialisation."""
        return type(self).__name__

    def to_data(self) -> dict[str, Any]:
        """Plain-JSON payload. Enum members degrade to their string values."""
        out: dict[str, Any] = {}
        for f in fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            out[f.name] = value.value if isinstance(value, enum.Enum) else value
        return out

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "DomainEvent":
        return cls(**data)  # type: ignore[call-arg]


@dataclass(frozen=True, slots=True)
class DetectionPolicyAdopted(DomainEvent):
    """Records which model and thresholds were in force for everything that follows.

    Written once at the head of the log so a replayer never needs to be *told*
    the baseline policy — it reads it back out of the evidence.
    """

    model_id: str
    alert_threshold: float
    rule_weight: float
    rules_only_floor: float
    fp_cost: float
    fn_cost: float
    auc: float
    avg_precision: float
    train_rows: int
    features: tuple[str, ...] | list[str]


@dataclass(frozen=True, slots=True)
class TransactionObserved(DomainEvent):
    """A transaction reached the sentinel.

    ``label``/``label_mode`` are populated only when the source is a labelled
    backtest export; a production feed records ``label=-1`` and ``label_mode=""``.
    Carrying them as part of the observation (rather than joining later) is what
    makes offline counterfactual scoring a pure fold over the log.
    """

    txn_id: int
    card_id: str
    occurred_at: float
    amount: float
    category: str
    lat: float
    lon: float
    hour: float
    is_online: int
    label: int
    label_mode: str


@dataclass(frozen=True, slots=True)
class RuleTriggered(DomainEvent):
    """One explainable rule fired, with the human-readable reason and its weight.

    The weight is recorded per-fire rather than looked up at read time so that
    re-weighting the rule book later cannot rewrite what the analyst saw.
    """

    txn_id: int
    card_id: str
    rule_name: str
    weight: float
    explanation: str


@dataclass(frozen=True, slots=True)
class RiskScored(DomainEvent):
    """The blended risk decision for one transaction under a stated policy."""

    txn_id: int
    card_id: str
    ml_score: float
    rule_score: float
    combined: float
    alert_threshold: float
    rule_weight: float


@dataclass(frozen=True, slots=True)
class AlertRaised(DomainEvent):
    """The policy escalated a scored transaction. ``trigger`` names which arm fired."""

    alert_id: str
    txn_id: int
    card_id: str
    occurred_at: float
    amount: float
    priority: float
    trigger: str


@dataclass(frozen=True, slots=True)
class CaseOpened(DomainEvent):
    """An alert entered the analyst queue."""

    case_id: str
    alert_id: str
    txn_id: int
    card_id: str
    opened_at: float
    priority: float
    amount: float
    headline: str


@dataclass(frozen=True, slots=True)
class CaseDispositioned(DomainEvent):
    """An analyst closed a case. This is the *only* way a case leaves the queue."""

    case_id: str
    disposition: str
    analyst: str
    note: str
    decided_at: float


def alert_id_for(txn_id: int) -> str:
    """Deterministic alert identity.

    Derived from the transaction rather than a UUID: replaying the same corpus
    must produce byte-identical events, and random ids would break that.
    """
    return f"ALERT-{txn_id:08d}"


def case_id_for(txn_id: int) -> str:
    """Deterministic case identity, for the same reason as :func:`alert_id_for`."""
    return f"CASE-{txn_id:08d}"


#: Wire-name -> class, used by the codec. Extending the vocabulary means adding here.
EVENT_TYPES: dict[str, type[DomainEvent]] = {
    cls.__name__: cls
    for cls in (
        DetectionPolicyAdopted,
        TransactionObserved,
        RuleTriggered,
        RiskScored,
        AlertRaised,
        CaseOpened,
        CaseDispositioned,
    )
}
