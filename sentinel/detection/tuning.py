"""Choosing the operating point on the score the policy actually compares.

This module exists because of a bug worth describing, since the shape of it recurs
everywhere a model is tuned in one place and applied in another.

``FraudModels`` tunes a threshold against its own probability. The alert policy does
not compare that probability: it compares ``(1 - rule_weight) * ml + rule_weight *
rules``. With ``rule_weight = 0.35`` the blend of a transaction no rule fired on tops
out at 0.65, so any tuned threshold above 0.65 silently makes those transactions
unalertable no matter how certain the model was. The demo's own counterfactual table
showed it: the recorded policy caught 43% of labelled fraud while "model only (rules
muted)" caught 99%, at a fraction of the cost.

The fix is not a fudge factor. It is to tune on the quantity that will be compared —
which means running the rule book over the evaluation rows, exactly as the live path
will, and sweeping the blended score.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..domain.policy import AlertPolicy, CostModel
from .rules import evaluate


@dataclass(frozen=True, slots=True)
class OperatingPoint:
    """The chosen cut, and what it would have cost on the evaluation rows."""

    threshold: float
    tp: int
    fp: int
    fn: int
    cost: float

    @property
    def precision(self) -> float:
        return round(self.tp / (self.tp + self.fp), 4) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return round(self.tp / (self.tp + self.fn), 4) if (self.tp + self.fn) else 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "threshold": round(self.threshold, 4),
            "blended_tp": self.tp,
            "blended_fp": self.fp,
            "blended_fn": self.fn,
            "blended_precision": self.precision,
            "blended_recall": self.recall,
            "blended_cost": round(self.cost, 2),
        }


def rule_scores(feat: pd.DataFrame) -> np.ndarray:
    """Rule score per row, computed by the same rule book the write side runs."""
    return np.array([evaluate(row)[0] for row in feat.to_dict("records")], dtype=float)


def tune_alert_threshold(
    ml_scores: np.ndarray,
    rules: np.ndarray,
    labels: np.ndarray,
    *,
    rule_weight: float,
    rules_only_floor: float,
    costs: CostModel,
    grid: int = 400,
) -> OperatingPoint:
    """Pick the blended-score cut that minimises expected cost.

    The candidate grid is the set of blended scores that actually occur, thinned to
    ``grid`` points: a threshold between two observed scores classifies identically to
    the lower of them, so nothing is lost by only considering realised values.

    Args:
        ml_scores: model probability per evaluation row.
        rules: rule score per row, from :func:`rule_scores`.
        labels: ground truth, 1 for fraud, 0 for genuine, negative for unlabelled.
        rule_weight: the blend the policy will use.
        rules_only_floor: the rules-only fast path, which alerts regardless of the cut.
        costs: what a false positive and a false negative are worth.
    """
    blended = (1.0 - rule_weight) * ml_scores + rule_weight * rules
    known = labels >= 0
    blended, rules, truth = blended[known], rules[known], labels[known] == 1
    if not len(blended):
        raise ValueError("cannot tune an operating point without labelled rows")

    candidates = np.unique(np.round(blended, 4))
    if len(candidates) > grid:
        candidates = candidates[:: max(1, len(candidates) // grid)]
    # The floor path alerts on its own, so it is part of every candidate's outcome.
    floor_fires = rules >= rules_only_floor

    best: OperatingPoint | None = None
    for threshold in candidates:
        fired = (blended >= threshold) | floor_fires
        tp = int((fired & truth).sum())
        fp = int((fired & ~truth).sum())
        fn = int((~fired & truth).sum())
        # A cut of exactly zero alerts on everything and is not a usable operating
        # point; the policy requires a positive threshold.
        point = OperatingPoint(max(float(threshold), 1e-4), tp, fp, fn, costs.expected(fp, fn))
        if best is None or point.cost < best.cost:
            best = point
    assert best is not None  # candidates is non-empty
    return best


def policy_for(point: OperatingPoint, *, rule_weight: float, rules_only_floor: float) -> AlertPolicy:
    """The policy the write side will adopt for this operating point."""
    return AlertPolicy(
        alert_threshold=point.threshold,
        rule_weight=rule_weight,
        rules_only_floor=rules_only_floor,
    )


__all__ = ["OperatingPoint", "policy_for", "rule_scores", "tune_alert_threshold"]
