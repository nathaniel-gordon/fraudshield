"""Per-case audit trail: the exact ordered facts that produced a case.

This is the artifact a chargeback dispute or a regulator actually asks for. It is not
generated, summarised or reconstructed from a report — it is the literal subsequence
of the log that concerns one transaction, with sequence numbers and chain hashes so
the reader can verify it against the archived file themselves.
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
from ..store.jsonl_store import EventStore


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

    Two passes are avoided by first resolving the case's transaction id from the
    ``CaseOpened`` record, then keeping any record that names either the case or
    that transaction. A single forward scan is enough because the log is ordered.
    """
    txn_id: int | None = None
    entries: list[TrailEntry] = []
    for rec in store.read():
        ev = rec.event
        if isinstance(ev, CaseOpened) and ev.case_id == case_id:
            txn_id = ev.txn_id
            entries.append(TrailEntry(rec.seq, rec.type, rec.hash, _describe(rec)))
            continue
        if isinstance(ev, CaseDispositioned) and ev.case_id == case_id:
            entries.append(TrailEntry(rec.seq, rec.type, rec.hash, _describe(rec)))
            continue
        if txn_id is None:
            # Buffer only while we do not yet know the transaction: keep a rolling
            # window of the current transaction's events so we can emit them once
            # the CaseOpened arrives.
            entries = _rolling(entries, rec)
            continue
        if getattr(ev, "txn_id", None) == txn_id:
            entries.append(TrailEntry(rec.seq, rec.type, rec.hash, _describe(rec)))
    # If the case was never opened, whatever the rolling buffer holds is unrelated.
    return entries if txn_id is not None else []


def _rolling(entries: list[TrailEntry], rec: Recorded) -> list[TrailEntry]:
    """Keep the pre-case events of the transaction currently being processed.

    A ``TransactionObserved`` starts a new group; rules and the score append to it.
    Anything else (an unrelated case disposition) leaves the group untouched.
    """
    ev = rec.event
    if isinstance(ev, TransactionObserved):
        return [TrailEntry(rec.seq, rec.type, rec.hash, _describe(rec))]
    if isinstance(ev, (RuleTriggered, RiskScored, AlertRaised)):
        entries.append(TrailEntry(rec.seq, rec.type, rec.hash, _describe(rec)))
    return entries


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
