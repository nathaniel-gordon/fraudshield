"""The analyst work queue — the read model the TUI renders.

Ordering is by priority descending, ties broken by case id so the queue is stable
across rebuilds. A case leaves the queue only via ``CaseDispositioned``; there is no
"delete" path, which is exactly the property a regulator asks about.

This projection is also the validator the ``DispositionCase`` command consults: CQRS
does not forbid the write side from *reading* a read model to enforce an invariant,
it forbids the write side from mutating one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..domain.events import (
    AlertRaised,
    CaseDispositioned,
    CaseOpened,
    Disposition,
    RiskScored,
    RuleTriggered,
)
from ..store.codec import Recorded
from .base import Projection, money, ratio


@dataclass(slots=True)
class CaseView:
    """One row of the queue, denormalised for display."""

    case_id: str
    alert_id: str
    txn_id: int
    card_id: str
    opened_at: float
    priority: float
    amount: float
    headline: str
    trigger: str
    ml_score: float
    rule_score: float
    reasons: tuple[str, ...]
    rules: tuple[str, ...]
    opened_seq: int
    disposition: str | None = None
    analyst: str | None = None
    note: str = ""
    decided_at: float | None = None
    decided_seq: int | None = None

    @property
    def is_open(self) -> bool:
        return self.disposition is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "alert_id": self.alert_id,
            "txn_id": self.txn_id,
            "card_id": self.card_id,
            "opened_at": self.opened_at,
            "priority": self.priority,
            "amount": money(self.amount),
            "headline": self.headline,
            "trigger": self.trigger,
            "ml_score": ratio(self.ml_score),
            "rule_score": ratio(self.rule_score),
            "reasons": list(self.reasons),
            "rules": list(self.rules),
            "opened_seq": self.opened_seq,
            "disposition": self.disposition,
            "analyst": self.analyst,
            "note": self.note,
            "decided_at": self.decided_at,
            "decided_seq": self.decided_seq,
        }


class CaseQueueProjection(Projection):
    """Open and closed cases, folded from alert and case events."""

    name = "case_queue"

    def __init__(self) -> None:
        self._cases: dict[str, CaseView] = {}
        self._order_dirty = True
        self._open_order: list[str] = []
        # Scoring context arrives before CaseOpened, so buffer it per transaction.
        self._scores: dict[int, tuple[float, float]] = {}
        self._fires: dict[int, list[tuple[str, str]]] = {}
        self._triggers: dict[int, str] = {}

    def apply(self, rec: Recorded) -> None:
        ev = rec.event
        if isinstance(ev, RuleTriggered):
            self._fires.setdefault(ev.txn_id, []).append((ev.rule_name, ev.explanation))
        elif isinstance(ev, RiskScored):
            self._scores[ev.txn_id] = (ev.ml_score, ev.rule_score)
        elif isinstance(ev, AlertRaised):
            self._triggers[ev.txn_id] = ev.trigger
        elif isinstance(ev, CaseOpened):
            ml, rules_score = self._scores.get(ev.txn_id, (0.0, 0.0))
            fires = self._fires.get(ev.txn_id, [])
            self._cases[ev.case_id] = CaseView(
                case_id=ev.case_id,
                alert_id=ev.alert_id,
                txn_id=ev.txn_id,
                card_id=ev.card_id,
                opened_at=ev.opened_at,
                priority=ev.priority,
                amount=ev.amount,
                headline=ev.headline,
                trigger=self._triggers.get(ev.txn_id, "ml_blend"),
                ml_score=ml,
                rule_score=rules_score,
                reasons=tuple(f"{name}: {why}" for name, why in fires),
                rules=tuple(name for name, _ in fires),
                opened_seq=rec.seq,
            )
            self._order_dirty = True
        elif isinstance(ev, CaseDispositioned):
            case = self._cases.get(ev.case_id)
            if case is None:
                return
            case.disposition = ev.disposition
            case.analyst = ev.analyst
            case.note = ev.note
            case.decided_at = ev.decided_at
            case.decided_seq = rec.seq
            self._order_dirty = True

    # ------------------------------------------------------------------ reads

    def _reorder(self) -> None:
        if not self._order_dirty:
            return
        self._open_order = [
            c.case_id
            for c in sorted(
                (c for c in self._cases.values() if c.is_open),
                key=lambda c: (-c.priority, c.case_id),
            )
        ]
        self._order_dirty = False

    def open_cases(self, limit: int | None = None) -> list[CaseView]:
        """Highest-priority open cases first."""
        self._reorder()
        ids = self._open_order if limit is None else self._open_order[:limit]
        return [self._cases[cid] for cid in ids]

    def closed_cases(self) -> list[CaseView]:
        """Dispositioned cases, newest decision first."""
        return sorted(
            (c for c in self._cases.values() if not c.is_open),
            key=lambda c: (-(c.decided_seq or 0), c.case_id),
        )

    def get(self, case_id: str) -> CaseView | None:
        return self._cases.get(case_id)

    def all_cases(self) -> Iterable[CaseView]:
        return self._cases.values()

    @property
    def open_count(self) -> int:
        return sum(1 for c in self._cases.values() if c.is_open)

    @property
    def closed_count(self) -> int:
        return len(self._cases) - self.open_count

    def outcome_counts(self) -> dict[str, int]:
        """Tally of analyst decisions so far."""
        counts = {d.value: 0 for d in Disposition}
        for case in self._cases.values():
            if case.disposition:
                counts[case.disposition] = counts.get(case.disposition, 0) + 1
        return counts

    def snapshot(self) -> dict[str, Any]:
        self._reorder()
        return {
            "open_order": list(self._open_order),
            "open_count": self.open_count,
            "closed_count": self.closed_count,
            "outcomes": self.outcome_counts(),
            "cases": {cid: self._cases[cid].to_dict() for cid in sorted(self._cases)},
        }
