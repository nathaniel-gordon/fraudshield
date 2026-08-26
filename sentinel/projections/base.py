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
        """Return the projection's full state as plain, JSON-safe data.

        Must be deterministic and order-stable: collections are sorted, floats are
        rounded at fixed precision. The replay-identity tests compare snapshots
        byte for byte, so any incidental ordering here would show up as a failure.
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
