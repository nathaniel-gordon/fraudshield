"""Counterfactual replay — "what would a different policy have caught?"

Tuning a fraud threshold normally means retraining, re-scoring and a week of waiting.
Here it costs one pass over the log, because the log already contains everything the
decision depended on: the model score, every rule that fired, its weight, and the
ground-truth label where the corpus carries one.

Crucially the replayer re-decides using the *same* :class:`AlertPolicy` object the
write side used, so a counterfactual configured with the recorded policy reproduces
the recorded alerts exactly — an equivalence the test suite asserts, and the reason
the alternative numbers can be trusted.

Nothing here writes. Production state is untouched; a counterfactual is a question,
not a change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..domain.events import AlertRaised, RiskScored, RuleTriggered, TransactionObserved
from ..domain.policy import AlertPolicy, rule_score_from
from ..store.sqlite_store import EventStore


@dataclass(frozen=True, slots=True)
class PolicyVariant:
    """A named what-if: a policy plus optional edits to the rule book."""

    label: str
    policy: AlertPolicy
    weight_overrides: Mapping[str, float] = field(default_factory=dict)
    disabled_rules: frozenset[str] = frozenset()

    def rule_score(self, fires: Mapping[str, float]) -> float:
        return rule_score_from(
            fires, weight_overrides=self.weight_overrides, disabled=self.disabled_rules
        )

    def describe(self) -> str:
        bits = [
            f"thr={self.policy.alert_threshold:.3f}",
            f"rw={self.policy.rule_weight:.2f}",
            f"floor={self.policy.rules_only_floor:.2f}",
        ]
        if self.disabled_rules:
            bits.append("off=" + ",".join(sorted(self.disabled_rules)))
        if self.weight_overrides:
            bits.append(
                "w=" + ",".join(f"{k}:{v:g}" for k, v in sorted(self.weight_overrides.items()))
            )
        return " ".join(bits)


@dataclass(slots=True)
class VariantResult:
    """Confusion counts and derived economics for one variant."""

    label: str
    description: str
    alerts: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    labelled_fraud: int = 0
    labelled_txns: int = 0
    matches_log: bool | None = None

    @property
    def precision(self) -> float:
        return round(self.tp / (self.tp + self.fp), 4) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return round(self.tp / self.labelled_fraud, 4) if self.labelled_fraud else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return round(2 * p * r / (p + r), 4) if (p + r) else 0.0

    def cost(self, fp_cost: float, fn_cost: float) -> float:
        """Expected operating cost: analyst hours burnt plus fraud losses taken."""
        return round(self.fp * fp_cost + self.fn * fn_cost, 2)

    def as_row(self, fp_cost: float, fn_cost: float) -> dict[str, Any]:
        return {
            "variant": self.label,
            "settings": self.description,
            "alerts": self.alerts,
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "cost": self.cost(fp_cost, fn_cost),
        }


def replay_variants(
    store: EventStore,
    variants: Sequence[PolicyVariant],
    *,
    verify_against_log: bool = True,
) -> list[VariantResult]:
    """Evaluate every variant in a single forward pass over the log.

    One pass rather than one pass per variant: a production log is large, and JSON
    decoding dominates, so the marginal cost of an extra what-if is a few float
    comparisons.

    Args:
        store: the log to replay.
        variants: policies to evaluate.
        verify_against_log: also check whether each variant's alert set is exactly
            the ``AlertRaised`` set already on the log. Exactly one variant — the
            recorded baseline — should come back ``True``.
    """
    results = [VariantResult(v.label, v.describe()) for v in variants]
    labels: dict[int, int] = {}
    fires: dict[int, dict[str, float]] = {}
    logged_alerts: set[int] = set()
    variant_alerts: list[set[int]] = [set() for _ in variants]

    for rec in store.read():
        ev = rec.event
        if isinstance(ev, TransactionObserved):
            labels[ev.txn_id] = ev.label
        elif isinstance(ev, RuleTriggered):
            fires.setdefault(ev.txn_id, {})[ev.rule_name] = ev.weight
        elif isinstance(ev, AlertRaised):
            logged_alerts.add(ev.txn_id)
        elif isinstance(ev, RiskScored):
            # RiskScored is the decision point: by construction every RuleTriggered
            # for this transaction has already been seen.
            txn_fires = fires.pop(ev.txn_id, {})
            label = labels.pop(ev.txn_id, -1)
            for variant, result, alerted in zip(variants, results, variant_alerts):
                rule_score = variant.rule_score(txn_fires)
                fired = variant.policy.should_alert(ev.ml_score, rule_score)
                if label >= 0:
                    result.labelled_txns += 1
                    if label == 1:
                        result.labelled_fraud += 1
                if fired:
                    result.alerts += 1
                    alerted.add(ev.txn_id)
                    if label == 1:
                        result.tp += 1
                    elif label == 0:
                        result.fp += 1
                elif label == 1:
                    result.fn += 1

    if verify_against_log:
        for result, alerted in zip(results, variant_alerts):
            result.matches_log = alerted == logged_alerts
    return results


def baseline_variant(policy: AlertPolicy, label: str = "baseline (as recorded)") -> PolicyVariant:
    """The variant that must reproduce the log exactly."""
    return PolicyVariant(label=label, policy=policy)


def standard_sweep(policy: AlertPolicy) -> list[PolicyVariant]:
    """A useful default what-if set around a recorded operating point.

    Chosen to probe the three levers an operations team actually has: how hard to
    push the score cut, how much to trust the rule book, and whether an individual
    rule is pulling its weight.
    """
    return [
        baseline_variant(policy),
        PolicyVariant("stricter cut (2x threshold)", policy.replace(alert_threshold=policy.alert_threshold * 2)),
        PolicyVariant("looser cut (0.5x threshold)", policy.replace(alert_threshold=policy.alert_threshold * 0.5)),
        PolicyVariant("rules-led blend (rw 0.60)", policy.replace(rule_weight=0.60)),
        PolicyVariant(
            "model only (rules muted)",
            policy.replace(rule_weight=0.0, rules_only_floor=2.0),
        ),
        PolicyVariant(
            "impossible_travel disabled",
            policy,
            disabled_rules=frozenset({"impossible_travel"}),
        ),
        PolicyVariant(
            "impossible_travel de-weighted 0.9->0.5",
            policy,
            weight_overrides={"impossible_travel": 0.5},
        ),
    ]


def sweep_rows(
    results: Iterable[VariantResult], fp_cost: float, fn_cost: float
) -> list[dict[str, Any]]:
    """Tabulate variant results for the report and the dashboard."""
    return [r.as_row(fp_cost, fn_cost) for r in results]
