"""Domain layer: immutable events, the alert policy value object, and clocks.

Nothing in here imports a store, a projection, a command handler or the UI. The
dependency arrow points inward only, so the vocabulary of the business stays
independent of how it happens to be persisted or displayed.
"""
from .clock import Clock, FrozenClock, SteppingClock
from .events import (
    EVENT_TYPES,
    AlertRaised,
    CaseDispositioned,
    CaseOpened,
    DetectionPolicyAdopted,
    Disposition,
    DomainEvent,
    RiskScored,
    RuleTriggered,
    TransactionObserved,
    alert_id_for,
    case_id_for,
)
from .policy import AlertPolicy, rule_score_from

__all__ = [
    "EVENT_TYPES",
    "AlertPolicy",
    "AlertRaised",
    "CaseDispositioned",
    "CaseOpened",
    "Clock",
    "DetectionPolicyAdopted",
    "Disposition",
    "DomainEvent",
    "FrozenClock",
    "RiskScored",
    "RuleTriggered",
    "SteppingClock",
    "TransactionObserved",
    "alert_id_for",
    "case_id_for",
    "rule_score_from",
]
