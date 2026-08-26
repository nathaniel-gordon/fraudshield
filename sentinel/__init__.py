"""Fraud Detection Sentinel — a CQRS + event-sourced card-fraud platform.

Layout mirrors the architecture, so the file tree names the pattern:

    domain/        immutable events, the alert policy, deterministic clocks
    detection/     the model ensemble, streaming features and the rule book
    commands/      the write side: commands, handlers, and the single-writer bus
    store/         the append-only, hash-chained JSONL event log
    projections/   the read side: read models built purely by folding the log
    query/         audit trails, time travel, counterfactual policy replay
    ui/            the ANSI analyst console and its live/scripted drivers

The one rule that holds the whole thing together: state changes only by appending an
event, and every read model is a pure fold over those events.
"""
from .domain.policy import AlertPolicy
from .projections.registry import ReadModel
from .store.jsonl_store import EventStore

__version__ = "2.0.0"
__all__ = ["AlertPolicy", "EventStore", "ReadModel", "__version__"]
