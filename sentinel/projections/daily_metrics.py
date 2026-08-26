"""Per-day operational metrics, folded from the log.

Days are derived from the transaction's own ``occurred_at``, never from wall-clock
time, so a replay run in 2031 buckets the 2026 traffic exactly as the original run
did. That is the difference between a metric and a coincidence.
"""
from __future__ import annotations

from typing import Any

from ..domain.events import (
    AlertRaised,
    CaseDispositioned,
    CaseOpened,
    Disposition,
    TransactionObserved,
)
from ..store.codec import Recorded
from .base import Projection, money, ratio

SECONDS_PER_DAY = 86400.0


class DailyMetricsProjection(Projection):
    """Daily volume, alert rate, money at risk, and confirmed loss avoided."""

    name = "daily_metrics"

    def __init__(self) -> None:
        self._days: dict[int, dict[str, float]] = {}
        #: case_id -> (day, amount), so a disposition can be attributed to its day.
        self._case_day: dict[str, tuple[int, float]] = {}
        self._labelled_fraud = 0
        self._labelled_total = 0

    def _day(self, index: int) -> dict[str, float]:
        return self._days.setdefault(
            index,
            {
                "txns": 0.0,
                "amount": 0.0,
                "labelled_fraud": 0.0,
                "alerts": 0.0,
                "amount_at_risk": 0.0,
                "cases_opened": 0.0,
                "confirmed_fraud": 0.0,
                "genuine": 0.0,
                "escalated": 0.0,
                "loss_avoided": 0.0,
            },
        )

    def apply(self, rec: Recorded) -> None:
        ev = rec.event
        if isinstance(ev, TransactionObserved):
            day = self._day(int(ev.occurred_at // SECONDS_PER_DAY))
            day["txns"] += 1
            day["amount"] += ev.amount
            if ev.label == 1:
                day["labelled_fraud"] += 1
                self._labelled_fraud += 1
            if ev.label >= 0:
                self._labelled_total += 1
        elif isinstance(ev, AlertRaised):
            day = self._day(int(ev.occurred_at // SECONDS_PER_DAY))
            day["alerts"] += 1
            day["amount_at_risk"] += ev.amount
        elif isinstance(ev, CaseOpened):
            index = int(ev.opened_at // SECONDS_PER_DAY)
            self._day(index)["cases_opened"] += 1
            self._case_day[ev.case_id] = (index, ev.amount)
        elif isinstance(ev, CaseDispositioned):
            located = self._case_day.get(ev.case_id)
            if located is None:
                return
            index, amount = located
            day = self._day(index)
            outcome = Disposition.parse(ev.disposition)
            if outcome is Disposition.CONFIRMED_FRAUD:
                day["confirmed_fraud"] += 1
                day["loss_avoided"] += amount
            elif outcome is Disposition.GENUINE:
                day["genuine"] += 1
            else:
                day["escalated"] += 1

    # ------------------------------------------------------------------ reads

    def rows(self) -> list[dict[str, Any]]:
        """Day-indexed rows in chronological order, ready to tabulate."""
        return [self._render(i, raw) for i, raw in sorted(self._days.items())]

    def totals(self) -> dict[str, Any]:
        """Whole-log rollup, including the labelled base rate when one exists."""
        txns = sum(d["txns"] for d in self._days.values())
        alerts = sum(d["alerts"] for d in self._days.values())
        return {
            "days": len(self._days),
            "txns": int(txns),
            "alerts": int(alerts),
            "alert_rate": ratio(alerts / txns) if txns else 0.0,
            "amount": money(sum(d["amount"] for d in self._days.values())),
            "amount_at_risk": money(sum(d["amount_at_risk"] for d in self._days.values())),
            "cases_opened": int(sum(d["cases_opened"] for d in self._days.values())),
            "confirmed_fraud": int(sum(d["confirmed_fraud"] for d in self._days.values())),
            "genuine": int(sum(d["genuine"] for d in self._days.values())),
            "escalated": int(sum(d["escalated"] for d in self._days.values())),
            "loss_avoided": money(sum(d["loss_avoided"] for d in self._days.values())),
            "labelled_fraud": self._labelled_fraud,
            "labelled_txns": self._labelled_total,
        }

    @staticmethod
    def _render(index: int, raw: dict[str, float]) -> dict[str, Any]:
        txns = raw["txns"] or 1.0
        return {
            "day": index,
            "txns": int(raw["txns"]),
            "amount": money(raw["amount"]),
            "labelled_fraud": int(raw["labelled_fraud"]),
            "alerts": int(raw["alerts"]),
            "alert_rate": ratio(raw["alerts"] / txns),
            "amount_at_risk": money(raw["amount_at_risk"]),
            "cases_opened": int(raw["cases_opened"]),
            "confirmed_fraud": int(raw["confirmed_fraud"]),
            "genuine": int(raw["genuine"]),
            "escalated": int(raw["escalated"]),
            "loss_avoided": money(raw["loss_avoided"]),
        }

    def snapshot(self) -> dict[str, Any]:
        return {"rows": self.rows(), "totals": self.totals()}
