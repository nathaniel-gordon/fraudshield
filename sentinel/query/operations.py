"""Analyst-facing queries, shaped for scripting rather than for a screen.

The TUI is the interface a human uses; this module is the one a shift lead pipes into
``jq``. Every function here returns plain data — no ANSI, no formatting decisions — so
the CLI can render it as a table or dump it as JSON from the same call.

Two things are deliberate:

* **Lists are paged.** An open queue is small today and unbounded tomorrow, and a
  command that prints an entire read model is a command nobody can use twice. Every
  list returns a :class:`Page` carrying the total and the offset to ask for next.
* **Nothing here writes.** These functions take a read model and a store and only
  fold or look up, which is why they are safe to run against a log another process is
  appending to.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import CaseNotFound, ValidationFailed
from ..projections.case_queue import CaseView
from ..projections.registry import ReadModel
from ..store.sqlite_store import EventStore
from .audit import case_trail

#: Refuse to build a page larger than this. A caller asking for a million rows wants
#: the JSONL export, not this.
MAX_PAGE = 500
DEFAULT_PAGE = 20


@dataclass(frozen=True, slots=True)
class Page:
    """One window onto a list, plus what it takes to ask for the next one."""

    items: list[dict[str, Any]]
    total: int
    offset: int
    limit: int

    @property
    def next_offset(self) -> int | None:
        """Offset of the following page, or ``None`` when this was the last."""
        following = self.offset + len(self.items)
        return following if following < self.total else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "next_offset": self.next_offset,
        }


def _paginate(rows: Sequence[Any], offset: int, limit: int) -> tuple[list[Any], int, int, int]:
    """Validate and apply a window, returning ``(slice, total, offset, limit)``."""
    if offset < 0:
        raise ValidationFailed("offset must not be negative", field="offset", value=offset)
    if limit < 1 or limit > MAX_PAGE:
        raise ValidationFailed(
            f"limit must be between 1 and {MAX_PAGE}", field="limit", value=limit
        )
    return list(rows[offset : offset + limit]), len(rows), offset, limit


def list_cases(
    read_model: ReadModel,
    *,
    status: str = "open",
    card_id: str = "",
    min_priority: float = 0.0,
    offset: int = 0,
    limit: int = DEFAULT_PAGE,
) -> Page:
    """The case queue as data.

    Args:
        status: ``open``, ``closed`` or ``all``.
        card_id: restrict to one card.
        min_priority: drop anything the queue ranks below this.
    """
    if status not in ("open", "closed", "all"):
        raise ValidationFailed(
            "status must be one of open, closed, all", field="status", value=status
        )
    if status == "open":
        rows: list[CaseView] = read_model.cases.open_cases()
    elif status == "closed":
        rows = read_model.cases.closed_cases()
    else:
        rows = sorted(read_model.cases.all_cases(), key=lambda c: (-c.priority, c.case_id))
    if card_id:
        rows = [c for c in rows if c.card_id == card_id]
    if min_priority > 0.0:
        rows = [c for c in rows if c.priority >= min_priority]
    window, total, offset, limit = _paginate(rows, offset, limit)
    return Page([case.to_dict() for case in window], total, offset, limit)


def case_detail(store: EventStore, read_model: ReadModel, case_id: str) -> dict[str, Any]:
    """One case, with the ordered facts behind it.

    Raises:
        CaseNotFound: the case was never opened in this log.
    """
    case = read_model.cases.get(case_id)
    if case is None:
        raise CaseNotFound(f"case {case_id!r} is not in this log", case_id=case_id)
    return {
        "case": case.to_dict(),
        "trail": [entry.as_dict() for entry in case_trail(store, case_id)],
    }


def card_report(
    store: EventStore,
    read_model: ReadModel,
    card_id: str,
    *,
    offset: int = 0,
    limit: int = DEFAULT_PAGE,
) -> dict[str, Any]:
    """A card's folded profile plus a page of its case history.

    Raises:
        CaseNotFound: the log holds no facts about this card. Reusing the query
            error rather than inventing a near-identical one keeps the taxonomy
            small; the code says ``case_not_found`` and the message says card.
    """
    profile = read_model.cards.get(card_id)
    stream_version = store.stream_version(f"card:{card_id}")
    if profile is None and not stream_version:
        raise CaseNotFound(f"no facts recorded for card {card_id!r}", card_id=card_id)
    cases = list_cases(read_model, status="all", card_id=card_id, offset=offset, limit=limit)
    return {
        "card_id": card_id,
        "profile": profile or {},
        "stream_version": stream_version,
        "cases": cases.as_dict(),
    }


def stats(read_model: ReadModel, *, since_day: int | None = None) -> dict[str, Any]:
    """Whole-log rollup plus the per-day rows, optionally from a day onwards."""
    rows = read_model.metrics.rows()
    if since_day is not None:
        rows = [row for row in rows if row["day"] >= since_day]
    return {
        "totals": read_model.metrics.totals(),
        "queue": {
            "open": read_model.cases.open_count,
            "closed": read_model.cases.closed_count,
            "outcomes": read_model.cases.outcome_counts(),
        },
        "days": rows,
        "upto_seq": read_model.upto_seq,
    }


def log_summary(store: EventStore) -> dict[str, Any]:
    """What the log itself is: size, head, schema, and the shape of its contents."""
    header = store.header()
    return {
        "path": str(store.path),
        "records": store.last_seq,
        "head_hash": store.head_hash,
        "schema_version": store.schema_version,
        "commands_applied": store.command_count(),
        "type_counts": store.type_counts(),
        "header": header.event.to_data() if header is not None else None,
    }


__all__ = [
    "DEFAULT_PAGE",
    "MAX_PAGE",
    "Page",
    "card_report",
    "case_detail",
    "list_cases",
    "log_summary",
    "stats",
]
