"""Deterministic domain clocks.

Wall-clock time is banned from the write side: an event log stamped with
``time.time()`` can never be replayed byte-identically, which would destroy the
central guarantee of this system. Handlers therefore take an explicit clock, and
the default one advances in fixed steps from the last observed *domain* time.
"""
from __future__ import annotations

from typing import Protocol


class Clock(Protocol):
    """Supplies the timestamp stamped onto analyst-originated events."""

    def now(self) -> float:
        """Return the current domain time in epoch-like seconds."""
        ...


class SteppingClock:
    """Monotonic clock that advances a fixed step on every read.

    Seeded from the last transaction time in the log so analyst decisions sort
    after the traffic that produced them, while staying perfectly reproducible.
    """

    __slots__ = ("_step", "_t")

    def __init__(self, start: float, step: float = 30.0) -> None:
        self._t = float(start)
        self._step = float(step)

    def now(self) -> float:
        self._t += self._step
        return round(self._t, 3)


class FrozenClock:
    """Always returns the same instant — used by tests that pin exact payloads."""

    __slots__ = ("_t",)

    def __init__(self, at: float) -> None:
        self._t = float(at)

    def now(self) -> float:
        return self._t
