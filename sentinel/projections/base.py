"""Contract every read model must honour.

A projection is a *fold*: `state = apply(state, event)` over the log, and nothing
else. It may not read a database, call a model, look at the clock or consult another
projection. That restriction is what buys the two properties the whole system rests
on — a projection can be rebuilt from scratch at any time, and it can be rebuilt as
of any past point.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

from ..store.codec import Recorded, canonical_json


class Projection(ABC):
    """Base class for read models built purely by replay."""

    #: Stable key under which this projection appears in a read-model snapshot.
    name: str = "projection"

    @abstractmethod
    def apply(self, rec: Recorded) -> None:
        """Fold one recorded event into the projection's state.

        Must be total (unknown event types are ignored, not an error) so that
        adding a new event type never breaks an existing read model.
        """

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Return the projection's reportable state as plain, JSON-safe data.

        Must be deterministic and order-stable: collections are sorted, floats are
        rounded at fixed precision. The replay-identity tests compare snapshots
        byte for byte, so any incidental ordering here would show up as a failure.

        This is a *view*, and it is deliberately lossy — money is rounded to cents,
        rates to six places, and the buffers a fold needs mid-stream are left out.
        Use :meth:`checkpoint` when the state has to be reloaded rather than read.
        """

    @abstractmethod
    def checkpoint(self) -> dict[str, Any]:
        """Return the projection's *complete* internal state, JSON-safe.

        The contract is exact round-tripping: restoring this into a fresh instance
        and folding the remaining events must land on the same state as folding the
        whole log from zero. That is stronger than :meth:`snapshot` guarantees, and
        it is why the two are separate methods — restoring from a rounded, partial
        view would quietly bend every number downstream of it.

        JSON object keys are strings, so integer-keyed maps are stringified here and
        parsed back in :meth:`restore`.
        """

    @abstractmethod
    def restore(self, state: Mapping[str, Any]) -> None:
        """Replace all internal state with one previously returned by ``checkpoint``.

        Must fully overwrite: a projection that merges instead of replacing would
        double-count whatever it already held.
        """

    def fingerprint(self) -> str:
        """SHA-256 of the canonical snapshot — a one-line equality proof."""
        return hashlib.sha256(canonical_json(self.snapshot()).encode("utf-8")).hexdigest()


def money(value: float) -> float:
    """Round to cents. Currency in a snapshot must never carry float noise."""
    return round(float(value) + 0.0, 2)


def ratio(value: float) -> float:
    """Round a rate to 6 places — enough for reporting, stable across platforms."""
    return round(float(value) + 0.0, 6)
