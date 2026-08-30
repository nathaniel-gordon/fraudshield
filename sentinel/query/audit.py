"""Per-case audit trail: the exact ordered facts that produced a case.

This is the artifact a chargeback dispute or a regulator actually asks for. It is not
generated, summarised or reconstructed from a report — it is the literal subsequence
of the log that concerns one transaction, with sequence numbers and chain hashes so
the reader can verify it against the archived file themselves.

Building it is two index lookups, not a scan. The log stores each record's ``txn_id``
and ``case_id`` alongside the payload precisely so that pulling one case's facts out
of a multi-hundred-thousand record log does not mean decoding all of it — which it
did, at roughly two seconds per keystroke, when this walked the whole file.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.events import (
    AlertRaised,
    CaseDispositioned,
    CaseOpened,
    RiskScored,
    RuleTriggered,
    TransactionObserved,
)
from ..store.codec import Recorded
from ..store.sqlite_store import EventStore


@dataclass(frozen=True, slots=True)
class TrailEntry:
    """One line of the audit trail."""

    seq: int
    type: str
    hash: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "hash": self.hash[:12], "detail": self.detail}


def case_trail(store: EventStore, case_id: str) -> list[TrailEntry]:
    """Collect, in log order, every fact that bears on ``case_id``.

    A case's facts are its own (``CaseOpened``, ``CaseDispositioned``) plus everything
    recorded against the transaction it was opened on. Both are indexed, so this is
    two lookups and a merge rather than a walk of the log.

    Returns:
        The trail in sequence order, or an empty list when the case was never opened.
    """
    txn_id = store.txn_of_case(case_id)
    if txn_id is None:
        return []
    by_seq: dict[int, Recorded] = {rec.seq: rec for rec in store.records_for_txn(txn_id)}
    for rec in store.records_for_case(case_id):
        by_seq[rec.seq] = rec
    return [
        TrailEntry(rec.seq, rec.type, rec.hash, _describe(rec))
        for _, rec in sorted(by_seq.items())
    ]


def _describe(rec: Recorded) -> str:
    """Human-readable one-liner for an event, for the TUI and the report."""
    ev = rec.event
    if isinstance(ev, TransactionObserved):
        label = {1: " [labelled FRAUD]", 0: " [labelled genuine]"}.get(ev.label, "")
        return (
            f"{ev.card_id} spent {ev.amount:.2f} on {ev.category} "
            f"({'online' if ev.is_online else 'in person'}) at t+{ev.occurred_at:,.0f}s"
            f"{label}"
        )
    if isinstance(ev, RuleTriggered):
        return f"{ev.rule_name} (+{ev.weight:.2f}) — {ev.explanation}"
    if isinstance(ev, RiskScored):
        return (
            f"ml={ev.ml_score:.4f} rules={ev.rule_score:.2f} blended={ev.combined:.4f} "
            f"vs threshold {ev.alert_threshold:.3f} (rule weight {ev.rule_weight:.2f})"
        )
    if isinstance(ev, AlertRaised):
        return f"alert {ev.alert_id} via {ev.trigger}, priority {ev.priority:.4f}"
    if isinstance(ev, CaseOpened):
        return f"case {ev.case_id} queued — {ev.headline}"
    if isinstance(ev, CaseDispositioned):
        note = f" — {ev.note}" if ev.note else ""
        return f"{ev.analyst} marked it {ev.disposition}{note}"
    return rec.type


__all__ = ["TrailEntry", "case_trail"]
