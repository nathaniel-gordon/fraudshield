"""Per-process counters and latency histograms.

Every ``python -m sentinel`` invocation is one short-lived process, so there is no
scrape target to keep warm and no daemon to query. What an operator actually wants
from a batch tool is the numbers for *this run*: how many events were appended, how
long the appends took, whether the rebuild came from a checkpoint. So the surface is
a registry that instrumented code writes into and one snapshot, dumped on request
(``--metrics``) as a single JSON document on stderr, next to the structured log lines.

Design constraints, in order:

* recording must be cheap enough to sit inside ``EventStore.append_groups`` — a
  lock, two adds and a bucket bump;
* the snapshot must be plain data (``json.dumps``-able, no custom types), because
  its whole purpose is to land in a pipeline;
* instruments are created on first use and named with dots (``store.append_ms``),
  so call sites never have to pre-register anything.

There is deliberately no push gateway, no server thread and no global sampling. If
this tool ever grows a long-running process, an exporter can read
:meth:`Registry.snapshot` — the data model (cumulative counters, bucketed
histograms) is the one every metrics backend already speaks.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

#: Upper bounds in milliseconds. Wide on purpose: the same buckets must make sense
#: for a sub-millisecond index lookup and a multi-second model fit.
DEFAULT_BUCKETS_MS: tuple[float, ...] = (
    0.5, 1, 2.5, 5, 10, 25, 50, 100, 250, 500, 1_000, 2_500, 5_000, 10_000, 30_000,
)


class Counter:
    """A monotonically increasing count. Thread-safe."""

    __slots__ = ("_lock", "_value")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def inc(self, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("counters only go up")
        with self._lock:
            self._value += amount

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


class Histogram:
    """Latency distribution over fixed buckets, in milliseconds. Thread-safe.

    Buckets are cumulative in the snapshot (each bound counts everything at or
    under it), which is the convention exporters expect and lets quantiles be
    estimated without keeping raw samples.
    """

    __slots__ = ("_bounds", "_counts", "_lock", "_max", "_min", "_n", "_sum")

    def __init__(self, buckets: Sequence[float] = DEFAULT_BUCKETS_MS) -> None:
        if list(buckets) != sorted(buckets) or len(set(buckets)) != len(buckets):
            raise ValueError("histogram buckets must be strictly increasing")
        self._lock = threading.Lock()
        self._bounds = tuple(float(b) for b in buckets)
        self._counts = [0] * (len(self._bounds) + 1)  # +1 for the overflow bucket
        self._n = 0
        self._sum = 0.0
        self._min: float | None = None
        self._max: float | None = None

    def observe(self, ms: float) -> None:
        value = float(ms)
        with self._lock:
            self._n += 1
            self._sum += value
            self._min = value if self._min is None else min(self._min, value)
            self._max = value if self._max is None else max(self._max, value)
            for i, bound in enumerate(self._bounds):
                if value <= bound:
                    self._counts[i] += 1
                    return
            self._counts[-1] += 1

    @contextmanager
    def time(self) -> Iterator[None]:
        """Observe the wall-clock duration of a block, even when it raises."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe((time.perf_counter() - start) * 1000.0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cumulative: dict[str, int] = {}
            running = 0
            for bound, count in zip(self._bounds, self._counts, strict=False):
                running += count
                cumulative[f"{bound:g}"] = running
            cumulative["+Inf"] = running + self._counts[-1]
            return {
                "count": self._n,
                "sum_ms": round(self._sum, 3),
                "min_ms": None if self._min is None else round(self._min, 3),
                "max_ms": None if self._max is None else round(self._max, 3),
                "buckets_ms": cumulative,
            }


class Registry:
    """Named instruments, created on first use.

    A name is either a counter or a histogram, never both; asking for the wrong
    kind is a programming error and raises immediately rather than silently
    splitting the series.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}

    def counter(self, name: str) -> Counter:
        with self._lock:
            if name in self._histograms:
                raise ValueError(f"{name!r} is already a histogram")
            return self._counters.setdefault(name, Counter())

    def histogram(self, name: str, buckets: Sequence[float] = DEFAULT_BUCKETS_MS) -> Histogram:
        with self._lock:
            if name in self._counters:
                raise ValueError(f"{name!r} is already a counter")
            return self._histograms.setdefault(name, Histogram(buckets))

    def snapshot(self) -> dict[str, Any]:
        """Everything recorded so far, as one JSON-ready document."""
        with self._lock:
            counters = dict(self._counters)
            histograms = dict(self._histograms)
        return {
            "counters": {name: c.value for name, c in sorted(counters.items())},
            "histograms": {name: h.snapshot() for name, h in sorted(histograms.items())},
        }

    def reset(self) -> None:
        """Drop every instrument. For tests; production code never rewinds a counter."""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


#: The process-wide registry every instrumented module writes into.
REGISTRY = Registry()


def counter(name: str) -> Counter:
    return REGISTRY.counter(name)


def histogram(name: str, buckets: Sequence[float] = DEFAULT_BUCKETS_MS) -> Histogram:
    return REGISTRY.histogram(name, buckets)


def snapshot() -> dict[str, Any]:
    return REGISTRY.snapshot()


__all__ = [
    "DEFAULT_BUCKETS_MS",
    "REGISTRY",
    "Counter",
    "Histogram",
    "Registry",
    "counter",
    "histogram",
    "snapshot",
]
