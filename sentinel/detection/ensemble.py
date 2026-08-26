"""Supervised + unsupervised model ensemble with cost-based threshold tuning."""
from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier, IsolationForest
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score

from .features import FEATURES


class FraudModels:
    def __init__(self, seed: int = 42, fp_cost: float = 12.0, fn_cost: float = 450.0):
        self.seed = seed
        self.fp_cost = fp_cost      # analyst time to clear a false alert
        self.fn_cost = fn_cost      # average loss of a missed fraud txn
        self.clf = GradientBoostingClassifier(random_state=seed, n_estimators=200, max_depth=3)
        self.iso = IsolationForest(random_state=seed, n_estimators=150, contamination="auto")
        self.threshold = 0.5
        self.metrics: dict = {}

    def train(self, feat: pd.DataFrame) -> dict:
        """Temporal split (first 70% train / last 30% test) — no future leakage."""
        feat = feat.sort_values("ts").reset_index(drop=True)
        cut = int(len(feat) * 0.7)
        tr, te = feat.iloc[:cut], feat.iloc[cut:]
        x_tr, y_tr = tr[FEATURES], tr["fraud"]
        x_te, y_te = te[FEATURES], te["fraud"]

        self.clf.fit(x_tr, y_tr)
        self.iso.fit(x_tr[y_tr == 0])
        raw_tr = -self.iso.score_samples(x_tr)
        self._iso_lo, self._iso_hi = np.percentile(raw_tr, [5, 99.5])

        proba = self._blend(x_te)
        auc = roc_auc_score(y_te, proba)
        ap = average_precision_score(y_te, proba)
        self.threshold = self._tune_threshold(y_te.to_numpy(), proba)
        pred = proba >= self.threshold
        tp = int((pred & (y_te == 1)).sum())
        fp = int((pred & (y_te == 0)).sum())
        fn = int((~pred & (y_te == 1)).sum())
        self.metrics = {
            "auc": round(float(auc), 4), "avg_precision": round(float(ap), 4),
            "threshold": round(float(self.threshold), 4),
            "precision": round(tp / (tp + fp), 3) if tp + fp else 0.0,
            "recall": round(tp / (tp + fn), 3) if tp + fn else 0.0,
            "test_frauds": int(y_te.sum()), "test_rows": len(te),
        }
        return self.metrics

    def _blend(self, x: pd.DataFrame) -> np.ndarray:
        """0.8 * supervised probability + 0.2 * normalized anomaly score."""
        p = self.clf.predict_proba(x)[:, 1]
        raw = -self.iso.score_samples(x)
        lo, hi = getattr(self, "_iso_lo", 0.0), getattr(self, "_iso_hi", 1.0)
        a = np.clip((raw - lo) / max(hi - lo, 1e-9), 0, 1)
        return 0.8 * p + 0.2 * a

    def _tune_threshold(self, y: np.ndarray, proba: np.ndarray) -> float:
        """Pick the threshold minimizing expected cost fp_cost*FP + fn_cost*FN."""
        _, _, thr = precision_recall_curve(y, proba)
        cands = np.unique(np.round(thr, 3))
        if len(cands) > 200:
            cands = cands[:: len(cands) // 200]
        best_t, best_cost = 0.5, float("inf")
        for t in cands:
            pred = proba >= t
            fp = int((pred & (y == 0)).sum())
            fn = int((~pred & (y == 1)).sum())
            cost = self.fp_cost * fp + self.fn_cost * fn
            if cost < best_cost:
                best_cost, best_t = cost, float(t)
        return best_t

    def score(self, x: pd.DataFrame) -> np.ndarray:
        return self._blend(x[FEATURES])

    def save(self, path: str | Path) -> None:
        joblib.dump(self, Path(path))

    @staticmethod
    def load(path: str | Path) -> "FraudModels":
        return joblib.load(Path(path))
