"""Synthetic card-transaction stream with labeled fraud modes.

Fraud modes injected:
  burst        - stolen card: rapid small-then-large purchases in a short window
  travel       - impossible travel: distant location seconds apart
  takeover     - account takeover: odd-hour high-value online spend, new category
Real deployments replace this with a transaction export (see README schema).

``hour`` is derived from ``ts`` and never set independently. It used to be assigned
per row and then overwritten wholesale by the derivation at the end, which silently
threw away the takeover mode's odd-hour signature — the one thing that distinguishes
it — so those rows only ever looked like ordinary large online purchases. A fraud
mode whose defining feature is the time of day has to move the timestamp.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CATEGORIES = ["grocery", "fuel", "restaurant", "online_retail", "electronics",
              "travel", "pharmacy", "entertainment", "utilities", "jewelry"]

CITY_GRID = {
    "home": (0.0, 0.0), "work": (0.15, 0.1), "mall": (-0.2, 0.25),
    "far_city": (8.0, 6.0), "abroad": (40.0, 30.0),
}


def generate_transactions(n_cards: int = 600, days: int = 45, seed: int = 42,
                          fraud_rate: float = 0.012) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    txn_id = 0
    for card in range(n_cards):
        home_lat, home_lon = rng.normal(0, 2), rng.normal(0, 2)
        spend_scale = rng.uniform(20, 90)
        n_txn = rng.poisson(days * 1.4)
        times = np.sort(rng.uniform(0, days * 24 * 3600, n_txn))
        fav_cats = rng.choice(CATEGORIES, size=3, replace=False)
        for t in times:
            cat = str(rng.choice(fav_cats) if rng.random() < 0.75 else rng.choice(CATEGORIES))
            spot = rng.choice(["home", "work", "mall"], p=[0.5, 0.3, 0.2])
            dlat, dlon = CITY_GRID[spot]
            rows.append({
                "txn_id": txn_id, "card_id": f"C{card:04d}", "ts": float(t),
                "amount": float(np.round(rng.gamma(2.2, spend_scale / 2.2), 2)),
                "category": cat,
                "lat": home_lat + dlat + rng.normal(0, 0.05),
                "lon": home_lon + dlon + rng.normal(0, 0.05),
                "is_online": int(rng.random() < 0.3),
                "fraud": 0, "fraud_mode": "",
            })
            txn_id += 1

    df = pd.DataFrame(rows)
    n_fraud_events = max(6, int(len(df) * fraud_rate / 3.2))  # each event = several txns
    fraud_cards = rng.choice(df["card_id"].unique(), size=n_fraud_events, replace=False)
    fraud_rows = []
    for card in fraud_cards:
        g = df[df["card_id"] == card]
        anchor = g.sample(1, random_state=int(rng.integers(1e9))).iloc[0]
        mode = str(rng.choice(["burst", "travel", "takeover"]))
        start = anchor["ts"] + rng.uniform(3600, 7200)
        if mode == "burst":
            amounts = [rng.uniform(1, 9)] + list(rng.uniform(180, 900, rng.integers(3, 6)))
            for i, amt in enumerate(amounts):
                fraud_rows.append(dict(anchor, txn_id=-1, ts=start + i * rng.uniform(60, 240),
                                       amount=round(float(amt), 2), category="electronics",
                                       is_online=1, fraud=1, fraud_mode=mode))
        elif mode == "travel":
            for i in range(int(rng.integers(2, 4))):
                far = CITY_GRID["far_city" if i % 2 == 0 else "abroad"]
                fraud_rows.append(dict(anchor, txn_id=-1, ts=start + i * rng.uniform(120, 600),
                                       amount=round(float(rng.uniform(90, 600)), 2),
                                       lat=anchor["lat"] + far[0], lon=anchor["lon"] + far[1],
                                       category="jewelry", fraud=1, fraud_mode=mode))
        else:  # takeover
            # Land the spree in the small hours by moving the clock, not the column:
            # odd_hour is int(hour < 6) and hour is derived from ts, so a hand-set
            # hour would be discarded and the signature would never reach the model.
            night = (start // 86400) * 86400 + float(rng.uniform(1.5, 3.0)) * 3600
            if night <= start:
                night += 86400
            for i in range(int(rng.integers(2, 5))):
                fraud_rows.append(dict(anchor, txn_id=-1,
                                       ts=night + i * rng.uniform(600, 1800),
                                       amount=round(float(rng.uniform(250, 1200)), 2),
                                       is_online=1,
                                       category=str(rng.choice(["online_retail", "electronics"])),
                                       fraud=1, fraud_mode=mode))
    fraud_df = pd.DataFrame(fraud_rows)
    df = pd.concat([df, fraud_df], ignore_index=True).sort_values("ts").reset_index(drop=True)
    df["txn_id"] = range(len(df))
    #: Single source of truth for the time of day. Every consumer — the odd_hour
    #: feature, the odd_hour_online_high_value rule — reads this column.
    df["hour"] = (df["ts"] / 3600) % 24
    return df
