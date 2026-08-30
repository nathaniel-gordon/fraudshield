"""The command bus — the single writer.

Everything that ever enters the log passes through here, in this order:

    command -> handler -> events -> store.append -> read model.apply

The read model is updated *from the records the store produced*, never from the
handler's return value directly. That one detail is what makes the live read model and
a from-scratch replay provably the same computation over the same input: both fold
exactly the records that exist on disk.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from ..projections.registry import ReadModel
from ..store.codec import Recorded
from ..store.sqlite_store import EventStore
from .handlers import CommandRejected, ObserveTransactionHandler
from .messages import ObserveTransaction

#: Transactions per model call during bulk ingest. Large enough to amortise the
#: scikit-learn call overhead, small enough that memory stays flat.
DEFAULT_BATCH = 2048


class CommandBus:
    """Routes commands to handlers and commits the resulting events."""

    def __init__(self, store: EventStore, read_model: ReadModel) -> None:
        self.store = store
        self.read_model = read_model
        self._handlers: dict[type, object] = {}
        self.accepted = 0
        self.rejected = 0

    def register(self, command_type: type, handler: object) -> "CommandBus":
        """Bind a command type to its handler. Returns self so wiring reads as a chain."""
        self._handlers[command_type] = handler
        return self

    def handler_for(self, command_type: type) -> object:
        handler = self._handlers.get(command_type)
        if handler is None:
            raise CommandRejected(f"no handler registered for {command_type.__name__}")
        return handler

    def submit(self, command: object) -> list[Recorded]:
        """Handle one command; append and project its events.

        Raises:
            CommandRejected: propagated from the handler, with nothing written.
        """
        handler = self.handler_for(type(command))
        events = handler.handle(command)  # type: ignore[attr-defined]
        records = self.store.append_many(events)
        for rec in records:
            self.read_model.apply(rec)
        self.accepted += 1
        return records

    def submit_many(
        self,
        commands: Iterable[ObserveTransaction],
        *,
        batch: int = DEFAULT_BATCH,
        progress=None,
    ) -> int:
        """Bulk-ingest observations, using the handler's vectorised path.

        Args:
            commands: time-ordered observation commands.
            batch: how many to score per model call.
            progress: optional ``callable(done, total_events)`` for the console.

        Returns:
            Total records appended.
        """
        handler = self.handler_for(ObserveTransaction)
        if not isinstance(handler, ObserveTransactionHandler):
            raise CommandRejected("submit_many requires the batching observation handler")
        total_records = 0
        done = 0
        for chunk in _chunks(list(commands), batch):
            per_command = handler.handle_batch(chunk)
            flat = [ev for events in per_command for ev in events]
            for rec in self.store.append_many(flat):
                self.read_model.apply(rec)
                total_records += 1
            done += len(chunk)
            self.accepted += len(chunk)
            if progress is not None:
                progress(done, total_records)
        return total_records


def _chunks(items: Sequence, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]
