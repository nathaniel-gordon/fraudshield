"""Commands — requests to change the world, which may be refused.

The asymmetry with events is deliberate and is the whole point of CQRS: a command is
an imperative in the present tense that can fail validation; an event is a past-tense
fact that already happened and can never be withdrawn. Commands are frozen so a
handler cannot quietly edit its own input on the way through.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..domain.events import Disposition


@dataclass(frozen=True, slots=True)
class ObserveTransaction:
    """Ingest one transaction and let the detector decide what it means."""

    txn_id: int
    card_id: str
    occurred_at: float
    amount: float
    category: str
    lat: float
    lon: float
    hour: float
    is_online: int
    label: int = -1
    label_mode: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ObserveTransaction":
        """Build from a transaction-export row.

        ``label``/``label_mode`` default to "unknown" so the same command works for a
        live feed and for a labelled backtest corpus without two code paths.
        """
        return cls(
            txn_id=int(row["txn_id"]),
            card_id=str(row["card_id"]),
            occurred_at=float(row["ts"]),
            amount=round(float(row["amount"]), 2),
            category=str(row["category"]),
            lat=float(row["lat"]),
            lon=float(row["lon"]),
            hour=float(row["hour"]),
            is_online=int(row["is_online"]),
            label=int(row["fraud"]) if row.get("fraud") is not None else -1,
            label_mode=str(row.get("fraud_mode") or ""),
        )

    def as_txn(self) -> dict[str, Any]:
        """Shape the detection layer expects (it predates the event vocabulary)."""
        return {
            "txn_id": self.txn_id,
            "card_id": self.card_id,
            "ts": self.occurred_at,
            "amount": self.amount,
            "category": self.category,
            "lat": self.lat,
            "lon": self.lon,
            "hour": self.hour,
            "is_online": self.is_online,
        }


@dataclass(frozen=True, slots=True)
class DispositionCase:
    """An analyst closes a case. Rejected if the case is unknown or already closed."""

    case_id: str
    disposition: Disposition
    analyst: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class AdoptDetectionPolicy:
    """Record the model and operating point governing everything that follows."""

    model_id: str
    alert_threshold: float
    rule_weight: float
    rules_only_floor: float
    fp_cost: float
    fn_cost: float
    auc: float
    avg_precision: float
    train_rows: int
    features: tuple[str, ...]
