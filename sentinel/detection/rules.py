"""Explainable rule book — the half of the detector a human can argue with.

The gradient-boosting ensemble is accurate but mute; a chargeback dispute needs a
sentence, not a probability. Each rule here returns a plain-English reason, and the
weight it contributes is recorded on the event log at fire time so that later
re-weighting cannot rewrite what an analyst was shown.

Thresholds and weights are unchanged from the pre-refactor detector: this module is
the proven science, only its output shape changed (structured fires instead of a
tuple of strings) so the command side can emit one ``RuleTriggered`` event per fire.
"""
from __future__ import annotations

from typing import Callable, NamedTuple


class RuleFire(NamedTuple):
    """One rule firing on one transaction."""

    name: str
    weight: float
    explanation: str


#: Registration order is the order reasons appear to the analyst — most severe first.
_REGISTERED: list[tuple[str, float, Callable[[dict], str | None]]] = []


def rule(name: str, weight: float):
    """Register a predicate as a weighted, explainable rule.

    The decorated function returns the reason string when it fires, else ``None``.
    """

    def deco(fn: Callable[[dict], str | None]) -> Callable[[dict], str | None]:
        _REGISTERED.append((name, weight, fn))
        return fn

    return deco


@rule("impossible_travel", 0.9)
def impossible_travel(f: dict) -> str | None:
    """A card cannot be in two cities at once — near-certain cloning."""
    if f["velocity_kmh"] > 900 and f["dist_km"] > 100:
        return f"card moved {f['dist_km']:.0f} km at {f['velocity_kmh']:.0f} km/h"
    return None


@rule("rapid_fire", 0.6)
def rapid_fire(f: dict) -> str | None:
    """Stolen cards get tested and drained fast, before the holder notices."""
    if f["txns_last_hour"] >= 4 and f["secs_since_last"] < 300:
        return f"{f['txns_last_hour']} txns in the last hour, {f['secs_since_last']:.0f}s gap"
    return None


@rule("amount_spike", 0.5)
def amount_spike(f: dict) -> str | None:
    """Spend far outside this card's own norm, not a global norm."""
    if f["amount_z"] > 4.5 and f["amount"] > 150:
        return f"amount {f['amount']:.2f} is {f['amount_z']:.1f} std above card norm"
    return None


@rule("odd_hour_online_high_value", 0.4)
def odd_hour_online_high_value(f: dict) -> str | None:
    """Classic account-takeover signature: night-time, online, well above normal."""
    if f["odd_hour"] and f["is_online"] and f["amount"] > 200 and f["amount_ratio"] > 3:
        return f"online {f['amount']:.2f} at night, {f['amount_ratio']:.1f}x usual spend"
    return None


@rule("new_category_high_value", 0.3)
def new_category_high_value(f: dict) -> str | None:
    """First-ever purchase in a category, at a value the card never reaches."""
    if f["new_category"] and f["amount_ratio"] > 4:
        return f"first purchase in category at {f['amount_ratio']:.1f}x usual spend"
    return None


def rule_book() -> tuple[tuple[str, float], ...]:
    """Registered rule names with their default weights, in firing order."""
    return tuple((name, weight) for name, weight, _ in _REGISTERED)


def evaluate(features: dict) -> tuple[float, tuple[RuleFire, ...]]:
    """Score a feature vector against the rule book.

    Returns:
        ``(rule_score in [0, 1], fires)``. The score is the capped sum of the
        weights of the rules that fired — the same arithmetic the counterfactual
        replayer applies to logged ``RuleTriggered`` events.
    """
    fires: list[RuleFire] = []
    total = 0.0
    for name, weight, fn in _REGISTERED:
        reason = fn(features)
        if reason:
            total += weight
            fires.append(RuleFire(name, weight, reason))
    return min(total, 1.0), tuple(fires)
