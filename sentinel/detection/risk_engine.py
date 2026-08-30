"""Write-side domain service: transaction -> risk assessment.

Deliberately *not* a projection. Scoring is a decision made once, at the moment a
transaction arrives, using the model and the per-card state that existed then. That
decision is recorded as a ``RiskScored`` fact; replaying the log must never re-score,
because the model file may have been retrained or lost since. This separation is the
reason the read side can be rebuilt on a machine that has no scikit-learn loaded.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pandas as pd

from .ensemble import FraudModels
from .features import FEATURES, FeatureStream
from .rules import RuleFire, evaluate


@dataclass(frozen=True, slots=True)
class Assessment:
    """What the detector concluded about one transaction."""

    ml_score: float
    rule_score: float
    fires: tuple[RuleFire, ...]


class RiskEngine:
    """Stateful scorer: owns the per-card streaming feature state.

    The state is genuinely online — feature extraction for transaction *n* sees only
    transactions 1..n-1 of that card — which is what makes the offline metrics an
    honest estimate of live performance.
    """

    def __init__(self, models: FraudModels) -> None:
        self.models = models
        self.stream = FeatureStream()

    def assess(self, txn: dict) -> Assessment:
        """Score a single transaction. Advances the per-card state."""
        return self.assess_batch([txn])[0]

    def assess_batch(self, txns: Sequence[dict]) -> list[Assessment]:
        """Score a micro-batch of time-ordered transactions.

        Semantically identical to calling :meth:`assess` in a loop — features are
        still extracted strictly one transaction at a time, in order — but the
        model is invoked once for the whole batch. A per-row ``predict_proba`` on
        38k transactions costs minutes; one vectorised call costs seconds.
        ``test_replay_identity`` asserts the two paths agree exactly.
        """
        if not txns:
            return []
        feats = [self.stream.extract(t) for t in txns]
        ml_scores = self.models.score(pd.DataFrame(feats)[FEATURES])
        out: list[Assessment] = []
        for feat, ml in zip(feats, ml_scores):
            rule_score, fires = evaluate(feat)
            out.append(Assessment(ml_score=round(float(ml), 6),
                                  rule_score=round(float(rule_score), 6),
                                  fires=fires))
        return out
