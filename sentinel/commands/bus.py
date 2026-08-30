"""The command bus — the single writer.

Everything that ever enters the log passes through here, in this order:

    command -> handler -> events -> store.append -> read model.apply

The read model is updated *from the records the store produced*, never from the
handler's return value directly. That one detail is what makes the live read model and
a from-scratch replay provably the same computation over the same input: both fold
exactly the records that exist on disk.

The bus is also where *at-most-once* lives. Every command carries a content-derived
id (see :mod:`sentinel.commands.identity`); the store records the sequence range each
id produced, under a UNIQUE constraint. A redelivery therefore cannot append a second
copy — the bus notices the id is known, returns the records the first delivery wrote,
and counts the command as a duplicate rather than an acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from ..errors import NoHandler
from ..obs import Timer, get_logger
from ..projections.registry import ReadModel
from ..store.codec import Recorded
from ..store.sqlite_store import CommandGroup, EventStore
from .handlers import ObserveTransactionHandler
from .identity import command_key
from .messages import ObserveTransaction

log = get_logger(__name__)

#: Transactions per model call during bulk ingest. Large enough to amortise the
#: scikit-learn call overhead, small enough that memory stays flat.
DEFAULT_BATCH = 2048


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """What one bulk submission did."""

    accepted: int
    duplicates: int
    records: int


class CommandBus:
    """Routes commands to handlers and commits the resulting events."""

    def __init__(self, store: EventStore, read_model: ReadModel) -> None:
        self.store = store
        self.read_model = read_model
        self._handlers: dict[type, object] = {}
        self.accepted = 0
        self.rejected = 0
        self.duplicates = 0

    def register(self, command_type: type, handler: object) -> "CommandBus":
        """Bind a command type to its handler. Returns self so wiring reads as a chain."""
        self._handlers[command_type] = handler
        return self

    def handler_for(self, command_type: type) -> object:
        handler = self._handlers.get(command_type)
        if handler is None:
            raise NoHandler(
                f"no handler registered for {command_type.__name__}",
                command_type=command_type.__name__,
            )
        return handler

    def submit(self, command: object) -> list[Recorded]:
        """Handle one command; append and project its events.

        A command whose id is already on the log is *not* re-executed: the records
        the first delivery produced are returned instead, so a caller retrying after
        a timeout sees the same answer it would have seen the first time.

        Raises:
            CommandRejected: propagated from the handler, with nothing written.
        """
        key = command_key(command)
        seen = self.store.command_range(key)
        if seen is not None:
            self.duplicates += 1
            log.info(
                "command_deduplicated",
                extra={"command": type(command).__name__, "command_id": key},
            )
            return self.store.records_between(*seen)

        handler = self.handler_for(type(command))
        events = handler.handle(command)  # type: ignore[attr-defined]
        records = self.store.append_many(
            events, command_id=key, command_type=type(command).__name__
        )
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
    ) -> BatchOutcome:
        """Bulk-ingest observations, using the handler's vectorised path.

        Each chunk is one transaction in the store, so the fsync count is
        proportional to chunks rather than to events while every chunk stays
        all-or-nothing. Commands already on the log are dropped before scoring, which
        is what makes a resumed ingest cheap as well as safe.

        Args:
            commands: time-ordered observation commands.
            batch: how many to score per model call.
            progress: optional ``callable(done, records_written)`` for the console.
        """
        handler = self.handler_for(ObserveTransaction)
        if not isinstance(handler, ObserveTransactionHandler):
            raise NoHandler("submit_many requires the batching observation handler")
        timer = Timer()
        accepted = duplicates = records_written = done = 0
        for chunk in _chunks(commands, batch):
            keys = [command_key(cmd) for cmd in chunk]
            known = self.store.known_commands(keys)
            fresh = [(key, cmd) for key, cmd in zip(keys, chunk) if key not in known]
            duplicates += len(chunk) - len(fresh)
            done += len(chunk)
            if fresh:
                per_command = handler.handle_batch([cmd for _, cmd in fresh])
                groups = [
                    CommandGroup(events, key, "ObserveTransaction")
                    for (key, _), events in zip(fresh, per_command)
                ]
                for rec in self.store.append_groups(groups):
                    self.read_model.apply(rec)
                    records_written += 1
                accepted += len(fresh)
            if progress is not None:
                progress(done, records_written)
        self.accepted += accepted
        self.duplicates += duplicates
        log.info(
            "batch_ingested",
            extra={
                "accepted": accepted,
                "duplicates": duplicates,
                "records": records_written,
                "elapsed_ms": timer.ms,
            },
        )
        return BatchOutcome(accepted=accepted, duplicates=duplicates, records=records_written)


def _chunks(items: Iterable, size: int) -> Iterator[list]:
    """Group an iterable into lists of ``size`` without materialising all of it."""
    chunk: list = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


__all__ = ["DEFAULT_BATCH", "BatchOutcome", "CommandBus"]
