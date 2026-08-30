"""The alerting policy as a pure, immutable value object.

This is the hinge of the whole design. The *write side* uses it to decide whether a
scored transaction becomes an alert; the *counterfactual replayer* uses the very same
object to re-decide historical scores under different settings. Because both paths
share one implementation, a counterfactual configured with the recorded policy
reproduces the recorded alerts exactly.

:class:`CostModel` lives here for the same reason. The cost of a false positive and of
a false negative decide the model's threshold, the sweep table, and the dashboard's
verdict on which policy is cheapest; they used to be written out as literals in four
places, which is three too many for numbers an operator is meant to be able to change.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: Below this the blended score is not interesting; above it a case is opened.
DEFAULT_RULE_WEIGHT = 0.35
#: Analyst time to clear one false alert, in dollars.
DEFAULT_FP_COST = 12.0
#: Average loss of one missed fraudulent transaction, in dollars.
DEFAULT_FN_COST = 450.0
#: A rules-only fast path: catastrophic patterns alert even when the model is unsure.
DEFAULT_RULES_ONLY_FLOOR = 0.85


@dataclass(frozen=True, slots=True)
class CostModel:
    """What a mistake costs, in dollars, in each direction.

    These are operating assumptions, not measurements: the defaults are the ones the
    detector's threshold was tuned against, and an operator with real numbers should
    pass their own via ``--fp-cost`` / ``--fn-cost``.
    """

    fp_cost: float = DEFAULT_FP_COST
    fn_cost: float = DEFAULT_FN_COST

    def expected(self, false_positives: int, false_negatives: int) -> float:
        """Total cost of a confusion outcome."""
        return self.fp_cost * false_positives + self.fn_cost * false_negatives

    def as_dict(self) -> dict[str, float]:
        return {"fp_cost": self.fp_cost, "fn_cost": self.fn_cost}


@dataclass(frozen=True, slots=True)
class AlertPolicy:
    """Immutable operating point of the detector.

    Args:
        alert_threshold: blended-score cut, tuned offline to minimise expected cost.
        rule_weight: how much of the blend comes from the explainable rules layer.
        rules_only_floor: rule score that alerts on its own, whatever the model says.
    """

    alert_threshold: float
    rule_weight: float = DEFAULT_RULE_WEIGHT
    rules_only_floor: float = DEFAULT_RULES_ONLY_FLOOR

    def combine(self, ml_score: float, rule_score: float) -> float:
        """Blend the model probability with the rule score."""
        return (1.0 - self.rule_weight) * ml_score + self.rule_weight * rule_score

    def should_alert(self, ml_score: float, rule_score: float) -> bool:
        """True when either the blend clears the threshold or the rules-only path fires."""
        return (
            self.combine(ml_score, rule_score) >= self.alert_threshold
            or rule_score >= self.rules_only_floor
        )

    def trigger_kind(self, ml_score: float, rule_score: float) -> str:
        """Name the arm responsible, for the analyst-facing explanation."""
        blend_fires = self.combine(ml_score, rule_score) >= self.alert_threshold
        rules_fire = rule_score >= self.rules_only_floor
        if blend_fires and rules_fire:
            return "hybrid"
        if rules_fire:
            return "rules_only"
        return "ml_blend"

    def priority(self, combined: float, amount: float) -> float:
        """Queue ordering: confidence tilted by money at risk.

        Analysts have finite hours, so a 0.9-confidence $1,200 case must outrank a
        0.9-confidence $12 case. The tilt is deliberately gentle (0.1% per dollar)
        so that amount nudges the ordering rather than dominating it.
        """
        return round(combined * (1.0 + 0.001 * amount), 4)

    def replace(self, **changes: float) -> AlertPolicy:
        """Return a variant; the original is untouched (value-object semantics)."""
        return AlertPolicy(
            alert_threshold=changes.get("alert_threshold", self.alert_threshold),
            rule_weight=changes.get("rule_weight", self.rule_weight),
            rules_only_floor=changes.get("rules_only_floor", self.rules_only_floor),
        )


def rule_score_from(
    fires: Mapping[str, float],
    *,
    weight_overrides: Mapping[str, float] | None = None,
    disabled: frozenset[str] = frozenset(),
) -> float:
    """Recompute a rule score from the rules that fired on a transaction.

    ``fires`` maps rule name -> the weight recorded at fire time. Overrides and
    disabling let a counterfactual re-run the rule book without re-running the
    (expensive, and possibly no-longer-available) model.
    """
    overrides = weight_overrides or {}
    total = 0.0
    for name, recorded_weight in fires.items():
        if name in disabled:
            continue
        total += float(overrides.get(name, recorded_weight))
    return min(total, 1.0)
