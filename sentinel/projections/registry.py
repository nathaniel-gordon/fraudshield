"""The read model: a bundle of projections folded over one event stream.

``ReadModel.rebuild(store)`` is the load-bearing method of this whole codebase. It
constructs every projection from nothing but the log, and the acceptance tests assert
that the result is byte-identical to the read model that was maintained live while the
commands were being handled. If that ever stops holding, some projection has grown
hidden state and the audit trail can no longer be trusted.
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable, Sequence

from ..store.codec import Recorded, canonical_json
from ..store.sqlite_store import EventStore
from .base import Projection
from .card_profile import CardProfileProjection
from .case_queue import CaseQueueProjection
from .daily_metrics import DailyMetricsProjection
from .rule_performance import RulePerformanceProjection


def default_projections() -> list[Projection]:
    """The four read models the dashboard and the reports are built on."""
    return [
        CardProfileProjection(),
        CaseQueueProjection(),
        DailyMetricsProjection(),
        RulePerformanceProjection(),
    ]


class ReadModel:
    """Query side. Never writes; only ever folds."""

    def __init__(self, projections: Sequence[Projection] | None = None) -> None:
        self._projections: list[Projection] = (
            list(projections) if projections is not None else default_projections()
        )
        self._by_name = {p.name: p for p in self._projections}
        self.applied = 0
        self.upto_seq = 0

    # ------------------------------------------------------------------ folds

    def apply(self, rec: Recorded) -> None:
        for projection in self._projections:
            projection.apply(rec)
        self.applied += 1
        self.upto_seq = rec.seq

    def apply_all(self, records: Iterable[Recorded]) -> "ReadModel":
        for rec in records:
            self.apply(rec)
        return self

    @classmethod
    def rebuild(cls, store: EventStore, upto_seq: int | None = None) -> "ReadModel":
        """Construct a fresh read model purely by replaying the log.

        Args:
            upto_seq: stop after this sequence number — the time-travel primitive.
        """
        return cls().apply_all(store.read(upto_seq=upto_seq))

    # ----------------------------------------------------------------- access

    @property
    def cases(self) -> CaseQueueProjection:
        return self._by_name["case_queue"]  # type: ignore[return-value]

    @property
    def cards(self) -> CardProfileProjection:
        return self._by_name["card_profiles"]  # type: ignore[return-value]

    @property
    def metrics(self) -> DailyMetricsProjection:
        return self._by_name["daily_metrics"]  # type: ignore[return-value]

    @property
    def rules(self) -> RulePerformanceProjection:
        return self._by_name["rule_performance"]  # type: ignore[return-value]

    def __getitem__(self, name: str) -> Projection:
        return self._by_name[name]

    def names(self) -> list[str]:
        return [p.name for p in self._projections]

    # ------------------------------------------------------------ equivalence

    def snapshot(self) -> dict[str, Any]:
        """Every projection's state under its own key, deterministically ordered."""
        return {name: self._by_name[name].snapshot() for name in sorted(self._by_name)}

    def fingerprint(self) -> str:
        """One hash covering the entire query side."""
        return hashlib.sha256(canonical_json(self.snapshot()).encode("utf-8")).hexdigest()

    def fingerprints(self) -> dict[str, str]:
        """Per-projection hashes, so a mismatch points at the guilty projection."""
        return {name: proj.fingerprint() for name, proj in sorted(self._by_name.items())}
