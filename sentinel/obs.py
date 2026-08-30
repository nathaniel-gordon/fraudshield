"""Structured logging with a correlation id per unit of work.

Every log line is one JSON object on **stderr**, so the TUI and the report tables keep
stdout to themselves and a pipeline can do ``python -m sentinel build 2> run.jsonl``.

The correlation id is a :mod:`contextvars` variable rather than an argument threaded
through every call: a command's rejection is logged deep inside a handler, and the CLI
invocation that caused it is three layers up. One id ties the CLI invocation, the
commands it submitted and the records it committed together in the log.

What is deliberately never logged: card identifiers in full, amounts, analyst notes,
and the seal key. A fraud log is full of things that must not leak into an ops
pipeline, so identifiers are masked at the edge by :func:`mask_card` and payload
fields are simply not passed.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: Fields the stdlib puts on every record; anything else is caller-supplied context.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)))

_correlation: ContextVar[str] = ContextVar("correlation_id", default="-")

ENV_LEVEL = "FRAUDSHIELD_LOG_LEVEL"
LOGGER_ROOT = "sentinel"


def new_correlation_id() -> str:
    """A fresh, short, unique id for one unit of work."""
    return uuid.uuid4().hex[:16]


def correlation_id() -> str:
    """The id in force on this task, or ``"-"`` outside any scope."""
    return _correlation.get()


@contextmanager
def correlation_scope(cid: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the duration of the block."""
    token = _correlation.set(cid or new_correlation_id())
    try:
        yield _correlation.get()
    finally:
        _correlation.reset(token)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the correlation id and any extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "correlation_id": _correlation.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "?"
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def configure_logging(level: str | int | None = None, stream=None) -> logging.Logger:
    """Install the JSON handler on the package logger. Idempotent.

    Args:
        level: explicit level; otherwise ``FRAUDSHIELD_LOG_LEVEL``; otherwise WARNING.
            The default is quiet on purpose — the CLI's own output is the interface,
            and an operator who wants the machine-readable trail asks for it.
    """
    logger = logging.getLogger(LOGGER_ROOT)
    resolved = level if level is not None else os.environ.get(ENV_LEVEL, "WARNING")
    logger.setLevel(resolved if isinstance(resolved, int) else str(resolved).upper())
    logger.propagate = False
    target = stream or sys.stderr
    for existing in list(logger.handlers):
        if getattr(existing, "_sentinel_handler", False):
            if existing.stream is target:  # type: ignore[attr-defined]
                return logger
            logger.removeHandler(existing)
    handler = logging.StreamHandler(target)
    handler.setFormatter(JsonFormatter())
    handler._sentinel_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    """Logger for a module, always under the package root."""
    suffix = name.rsplit(".", maxsplit=1)[-1]
    return logging.getLogger(f"{LOGGER_ROOT}.{suffix}")


def mask_card(card_id: str) -> str:
    """Card identifiers are logged truncated — the log line is not the evidence."""
    if not card_id:
        return ""
    if len(card_id) <= 2:
        return "*" * len(card_id)
    return f"{card_id[:2]}{'*' * (len(card_id) - 2)}"


class Timer:
    """Wall-clock stopwatch for log fields. Never used for anything the log records."""

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def ms(self) -> float:
        return round((time.perf_counter() - self._start) * 1000.0, 2)


__all__ = [
    "JsonFormatter",
    "Timer",
    "configure_logging",
    "correlation_id",
    "correlation_scope",
    "get_logger",
    "mask_card",
    "new_correlation_id",
]
