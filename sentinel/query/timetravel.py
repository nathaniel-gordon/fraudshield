"""Time travel — "what did we know when this alert fired?"

The single most common complaint about a fraud decision is that it looks obviously
wrong *in hindsight*. Hindsight is not the standard the decision was made under. A
projection that is a pure fold can be rebuilt over any prefix of the log, so this
module reconstructs the exact read model that existed at a given sequence number and
lets the reviewer judge the decision on the evidence available at the time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import CaseNotFound
from ..projections.checkpoints import rebuild_with_checkpoints
from ..projections.registry import ReadModel
from ..store.sqlite_store import EventStore


def read_model_as_of(store: EventStore, seq: int) -> ReadModel:
    """Rebuild every projection over records ``1..seq`` inclusive.

    Resumes from a stored checkpoint when the log has a usable one — this view is
    reached by a keystroke in the console, so the whole prefix was being folded each
    time it was opened. The result is the same either way; a checkpoint that cannot
    be trusted is discarded and the fold starts at record 1.
    """
    return rebuild_with_checkpoints(store, upto_seq=seq)


def seq_of_case(store: EventStore, case_id: str) -> int | None:
    """Where in the log a given case was opened.

    Delegates to the store's ``case_id`` index. Finding this by scanning cost a
    full pass over the log every time the analyst pressed ``t``.
    """
    return store.seq_of_case(case_id)


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
        CaseNotFound: if the case is not in the log.
    """
    at = seq_of_case(store, case_id)
    if at is None:
        raise CaseNotFound(f"case {case_id!r} was never opened in this log", case_id=case_id)
    past = read_model_as_of(store, at)
    case = past.cases.get(case_id)
    if case is None:  # pragma: no cover - guaranteed by seq_of_case
        raise CaseNotFound(f"case {case_id!r} missing from as-of read model", case_id=case_id)
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


__all__ = ["KnowledgeSnapshot", "knowledge_at_case", "read_model_as_of", "seq_of_case"]
