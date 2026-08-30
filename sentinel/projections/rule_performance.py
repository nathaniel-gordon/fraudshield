"""Which rules actually earn their keep.

Two precisions are tracked, because they answer different questions:

* ``label_precision`` — of the transactions this rule fired on, what fraction were
  fraud according to the labelled corpus? This is the model-development view.
* ``analyst_precision`` — of the *cases* this rule contributed to that a human has
  closed, what fraction were confirmed fraud? This is the operations view, and it
  only becomes available as dispositions accumulate on the log.

Both are folds. No join against a warehouse, no nightly batch — the facts needed are
already in the stream, which is precisely why they were made events.
"""
from __future__ import annotations

from typing import Any

from ..domain.events import (
    CaseDispositioned,
    CaseOpened,
    Disposition,
    RuleTriggered,
    TransactionObserved,
)
from ..store.codec import Recorded
from .base import Projection, ratio


class RulePerformanceProjection(Projection):
    """Per-rule fire counts, label precision and analyst-confirmed precision."""

    name = "rule_performance"

    def __init__(self) -> None:
        self._rules: dict[str, dict[str, Any]] = {}
        #: txn_id -> label, only kept for transactions a rule actually fired on.
        self._labels: dict[int, int] = {}
        #: txn_id -> rules that fired, used to attribute a disposition backwards.
        self._txn_rules: dict[int, set[str]] = {}
        self._case_txn: dict[str, int] = {}
        self._pending_labels: dict[int, int] = {}

    def _rule(self, name: str) -> dict[str, Any]:
        return self._rules.setdefault(
            name,
            {
                "fires": 0,
                "weight": 0.0,
                "fires_on_labelled_fraud": 0,
                "fires_on_labelled_txn": 0,
                "cases": 0,
                "closed": 0,
                "confirmed_fraud": 0,
                "genuine": 0,
                "escalated": 0,
            },
        )

    def apply(self, rec: Recorded) -> None:
        ev = rec.event
        if isinstance(ev, TransactionObserved):
            # Labels are buffered rather than stored for every transaction: only
            # those a rule touches are ever needed, and a production log has
            # millions of clean transactions we must not hold in memory.
            self._pending_labels[ev.txn_id] = ev.label
        elif isinstance(ev, RuleTriggered):
            entry = self._rule(ev.rule_name)
            entry["fires"] += 1
            entry["weight"] = ev.weight
            label = self._pending_labels.pop(ev.txn_id, self._labels.get(ev.txn_id, -1))
            self._labels[ev.txn_id] = label
            if label >= 0:
                entry["fires_on_labelled_txn"] += 1
                if label == 1:
                    entry["fires_on_labelled_fraud"] += 1
            self._txn_rules.setdefault(ev.txn_id, set()).add(ev.rule_name)
        elif isinstance(ev, CaseOpened):
            self._case_txn[ev.case_id] = ev.txn_id
            for name in self._txn_rules.get(ev.txn_id, ()):
                self._rule(name)["cases"] += 1
        elif isinstance(ev, CaseDispositioned):
            txn_id = self._case_txn.get(ev.case_id)
            if txn_id is None:
                return
            outcome = Disposition.parse(ev.disposition)
            for name in self._txn_rules.get(txn_id, ()):
                entry = self._rule(name)
                entry["closed"] += 1
                if outcome is Disposition.CONFIRMED_FRAUD:
                    entry["confirmed_fraud"] += 1
                elif outcome is Disposition.GENUINE:
                    entry["genuine"] += 1
                else:
                    entry["escalated"] += 1
        # Bound the label buffer: transactions no rule touched are never needed.
        if len(self._pending_labels) > 4096:
            self._pending_labels.clear()

    # ------------------------------------------------------------------ reads

    def rows(self) -> list[dict[str, Any]]:
        """Rules ordered by fire count, richest signal first."""
        return sorted(
            (self._render(name, raw) for name, raw in self._rules.items()),
            key=lambda r: (-r["fires"], r["rule"]),
        )

    @staticmethod
    def _render(name: str, raw: dict[str, Any]) -> dict[str, Any]:
        labelled = raw["fires_on_labelled_txn"]
        closed = raw["closed"]
        decided = raw["confirmed_fraud"] + raw["genuine"]
        return {
            "rule": name,
            "weight": raw["weight"],
            "fires": raw["fires"],
            "cases": raw["cases"],
            "fires_on_labelled_fraud": raw["fires_on_labelled_fraud"],
            "label_precision": ratio(raw["fires_on_labelled_fraud"] / labelled) if labelled else 0.0,
            "closed": closed,
            "confirmed_fraud": raw["confirmed_fraud"],
            "genuine": raw["genuine"],
            "escalated": raw["escalated"],
            "analyst_precision": ratio(raw["confirmed_fraud"] / decided) if decided else 0.0,
        }

    def snapshot(self) -> dict[str, Any]:
        return {"rules": self.rows()}
