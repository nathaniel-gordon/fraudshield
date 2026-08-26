"""Time travel — "what did we know when this alert fired?"

The single most common complaint about a fraud decision is that it looks obviously
wrong *in hindsight*. Hindsight is not the standard the decision was made under. A
projection that is a pure fold can be rebuilt over any prefix of the log, so this
module reconstructs the exact read model that existed at a given sequence number and
lets the reviewer judge the decision on the evidence available at the time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..domain.events import AlertRaised, CaseOpened
from ..projections.registry import ReadModel
from ..store.codec import Recorded
from ..store.jsonl_store import EventStore


def read_model_as_of(store: EventStore, seq: int) -> ReadModel:
    """Rebuild every projection over records ``1..seq`` inclusive."""
    return ReadModel.rebuild(store, upto_seq=seq)


def seq_where(store: EventStore, predicate: Callable[[Recorded], bool]) -> int | None:
    """Sequence number of the first record satisfying ``predicate``."""
    for rec in store.read():
        if predicate(rec):
            return rec.seq
    return None


def seq_of_alert(store: EventStore, alert_id: str) -> int | None:
    """Where in the log a given alert was raised."""
    return seq_where(
        store, lambda r: isinstance(r.event, AlertRaised) and r.event.alert_id == alert_id
    )


def seq_of_case(store: EventStore, case_id: str) -> int | None:
    """Where in the log a given case was opened."""
    return seq_where(
        store, lambda r: isinstance(r.event, CaseOpened) and r.event.case_id == case_id
    )


@dataclass(frozen=True, slots=True)
class KnowledgeSnapshot:
    """What the sentinel knew about a card at one point in the log, versus now."""

    case_id: str
    card_id: str
    at_seq: int
    now_seq: int
    then: dict[str, Any]
    now: dict[str, Any]
    queue_depth_then: int
    queue_depth_now: int

    def deltas(self) -> dict[str, tuple[Any, Any]]:
        """Fields that changed between then and now, as ``(then, now)`` pairs."""
        keys = ("txns", "total_amount", "avg_amount", "max_amount", "alerts",
                "open_cases", "confirmed_fraud", "genuine", "amount_at_risk")
        return {
            k: (self.then.get(k), self.now.get(k))
            for k in keys
            if self.then.get(k) != self.now.get(k)
        }


def knowledge_at_case(store: EventStore, case_id: str, live: ReadModel) -> KnowledgeSnapshot:
    """Reconstruct the card's profile as of the moment its case was opened.

    Args:
        store: the log to replay.
        case_id: the case under review.
        live: the current read model, used for the "now" side of the comparison.

    Raises:
        KeyError: if the case is not in the log.
    """
    at = seq_of_case(store, case_id)
    if at is None:
        raise KeyError(f"case {case_id!r} was never opened in this log")
    past = read_model_as_of(store, at)
    case = past.cases.get(case_id)
    if case is None:  # pragma: no cover - guaranteed by seq_of_case
        raise KeyError(f"case {case_id!r} missing from as-of read model")
    return KnowledgeSnapshot(
        case_id=case_id,
        card_id=case.card_id,
        at_seq=at,
        now_seq=live.upto_seq,
        then=past.cards.get(case.card_id) or {},
        now=live.cards.get(case.card_id) or {},
        queue_depth_then=past.cases.open_count,
        queue_depth_now=live.cases.open_count,
    )
