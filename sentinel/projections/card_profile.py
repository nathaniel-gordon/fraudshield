"""Per-card behavioural summary, folded from the log.

Answers the first question an analyst asks about an alert: *is this card normally
like this?* Because the projection is a pure fold, the same class rebuilt with
``upto_seq`` set gives the profile as it stood when the alert fired — which is the
only fair basis on which to judge the decision.
"""
from __future__ import annotations

from typing import Any

from ..domain.events import (
    AlertRaised,
    CaseDispositioned,
    CaseOpened,
    Disposition,
    RuleTriggered,
    TransactionObserved,
)
from ..store.codec import Recorded
from .base import Projection, money


class CardProfileProjection(Projection):
    """Card-level counters, spend totals and case outcomes."""

    name = "card_profiles"

    def __init__(self) -> None:
        self._cards: dict[str, dict[str, Any]] = {}
        #: case_id -> card_id, needed because CaseDispositioned carries no card.
        self._case_card: dict[str, str] = {}

    def _card(self, card_id: str) -> dict[str, Any]:
        return self._cards.setdefault(
            card_id,
            {
                "txns": 0,
                "total_amount": 0.0,
                "max_amount": 0.0,
                "categories": set(),
                "first_seen": None,
                "last_seen": None,
                "rule_fires": {},
                "alerts": 0,
                "open_cases": 0,
                "confirmed_fraud": 0,
                "genuine": 0,
                "escalated": 0,
                "amount_at_risk": 0.0,
            },
        )

    def apply(self, rec: Recorded) -> None:
        ev = rec.event
        if isinstance(ev, TransactionObserved):
            card = self._card(ev.card_id)
            card["txns"] += 1
            card["total_amount"] += ev.amount
            card["max_amount"] = max(card["max_amount"], ev.amount)
            card["categories"].add(ev.category)
            if card["first_seen"] is None:
                card["first_seen"] = ev.occurred_at
            card["last_seen"] = ev.occurred_at
        elif isinstance(ev, RuleTriggered):
            fires = self._card(ev.card_id)["rule_fires"]
            fires[ev.rule_name] = fires.get(ev.rule_name, 0) + 1
        elif isinstance(ev, AlertRaised):
            card = self._card(ev.card_id)
            card["alerts"] += 1
            card["amount_at_risk"] += ev.amount
        elif isinstance(ev, CaseOpened):
            self._case_card[ev.case_id] = ev.card_id
            self._card(ev.card_id)["open_cases"] += 1
        elif isinstance(ev, CaseDispositioned):
            card_id = self._case_card.get(ev.case_id)
            if card_id is None:
                return
            card = self._card(card_id)
            card["open_cases"] = max(0, card["open_cases"] - 1)
            outcome = Disposition.parse(ev.disposition)
            if outcome is Disposition.CONFIRMED_FRAUD:
                card["confirmed_fraud"] += 1
            elif outcome is Disposition.GENUINE:
                card["genuine"] += 1
            else:
                card["escalated"] += 1

    def get(self, card_id: str) -> dict[str, Any] | None:
        """Public read: one card's profile, JSON-safe, or ``None`` if unseen."""
        raw = self._cards.get(card_id)
        return None if raw is None else self._render(raw)

    def riskiest(self, limit: int = 10) -> list[tuple[str, dict[str, Any]]]:
        """Cards ordered by open alert volume then money at risk."""
        ranked = sorted(
            self._cards.items(),
            key=lambda kv: (-kv[1]["alerts"], -kv[1]["amount_at_risk"], kv[0]),
        )
        return [(cid, self._render(raw)) for cid, raw in ranked[:limit]]

    def __len__(self) -> int:
        return len(self._cards)

    @staticmethod
    def _render(raw: dict[str, Any]) -> dict[str, Any]:
        txns = raw["txns"] or 1
        return {
            "txns": raw["txns"],
            "total_amount": money(raw["total_amount"]),
            "avg_amount": money(raw["total_amount"] / txns),
            "max_amount": money(raw["max_amount"]),
            "categories": sorted(raw["categories"]),
            "first_seen": raw["first_seen"],
            "last_seen": raw["last_seen"],
            "rule_fires": dict(sorted(raw["rule_fires"].items())),
            "alerts": raw["alerts"],
            "open_cases": raw["open_cases"],
            "confirmed_fraud": raw["confirmed_fraud"],
            "genuine": raw["genuine"],
            "escalated": raw["escalated"],
            "amount_at_risk": money(raw["amount_at_risk"]),
        }

    def snapshot(self) -> dict[str, Any]:
        return {cid: self._render(raw) for cid, raw in sorted(self._cards.items())}
