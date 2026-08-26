"""Online per-card state and feature extraction (stream-safe: uses only past data)."""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

EARTH_KM = 111.0  # km per degree (flat approximation adequate for velocity flags)

FEATURES = ["amount", "amount_ratio", "log_amount", "secs_since_last", "dist_km",
            "velocity_kmh", "txns_last_hour", "txns_last_day", "new_category",
            "odd_hour", "is_online", "amount_z"]


@dataclass
class CardState:
    last_ts: float | None = None
    last_lat: float = 0.0
    last_lon: float = 0.0
    amounts: deque = field(default_factory=lambda: deque(maxlen=50))
    recent_ts: deque = field(default_factory=lambda: deque(maxlen=200))
    categories: set = field(default_factory=set)

    def mean_amount(self) -> float:
        return float(np.mean(self.amounts)) if self.amounts else 0.0

    def std_amount(self) -> float:
        return float(np.std(self.amounts)) if len(self.amounts) > 3 else 0.0


class FeatureStream:
    """Consumes time-ordered transactions, emits features from per-card running state."""

    def __init__(self) -> None:
        self.state: dict[str, CardState] = {}

    def extract(self, txn: dict) -> dict:
        s = self.state.setdefault(txn["card_id"], CardState())
        secs = (txn["ts"] - s.last_ts) if s.last_ts is not None else 86400.0
        secs = max(secs, 1.0)
        dist = math.hypot(txn["lat"] - s.last_lat, txn["lon"] - s.last_lon) * EARTH_KM \
            if s.last_ts is not None else 0.0
        velocity = dist / (secs / 3600.0)
        mean_amt = s.mean_amount()
        std_amt = s.std_amount()
        hour_ago, day_ago = txn["ts"] - 3600, txn["ts"] - 86400
        feat = {
            "amount": txn["amount"],
            "amount_ratio": txn["amount"] / mean_amt if mean_amt > 0 else 1.0,
            "log_amount": math.log1p(txn["amount"]),
            "secs_since_last": secs,
            "dist_km": dist,
            "velocity_kmh": min(velocity, 5000.0),
            "txns_last_hour": sum(1 for t in s.recent_ts if t >= hour_ago),
            "txns_last_day": sum(1 for t in s.recent_ts if t >= day_ago),
            "new_category": int(txn["category"] not in s.categories and len(s.categories) >= 2),
            "odd_hour": int(txn["hour"] < 6),
            "is_online": int(txn["is_online"]),
            "amount_z": (txn["amount"] - mean_amt) / std_amt if std_amt > 0 else 0.0,
        }
        s.last_ts, s.last_lat, s.last_lon = txn["ts"], txn["lat"], txn["lon"]
        s.amounts.append(txn["amount"])
        s.recent_ts.append(txn["ts"])
        s.categories.add(txn["category"])
        return feat


def build_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Batch helper: run the online extractor over a time-ordered frame."""
    fs = FeatureStream()
    feats = [fs.extract(row) for row in df.sort_values("ts").to_dict("records")]
    out = pd.DataFrame(feats)
    for col in ("txn_id", "card_id", "ts", "fraud", "fraud_mode"):
        if col in df.columns:
            out[col] = df.sort_values("ts")[col].to_numpy()
    return out
