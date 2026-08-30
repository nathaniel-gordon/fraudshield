"""One error taxonomy for the whole system.

Every failure this codebase raises on purpose descends from :class:`SentinelError` and
carries a stable ``code``. The code is what the CLI prints in ``--json`` mode, what the
structured log records, and what a caller should branch on — the message is for humans
and is free to change.

Three families, matching the three things that can go wrong:

    StoreError      the log itself is unhappy (broken chain, racing writer, torn file)
    CommandRejected the write side refused an instruction; nothing was appended
    QueryError      the read side was asked about something that is not there

``CommandRejected`` deliberately is *not* an event. A refused instruction is a
non-event, and appending it would pollute the fact stream every projection folds.
"""
from __future__ import annotations

from typing import Any


class SentinelError(Exception):
    """Base class. ``code`` is the stable machine-readable discriminator."""

    code = "sentinel_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form, used by the CLI's ``--json`` mode."""
        out: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out

    def log_fields(self) -> dict[str, Any]:
        """Same content under keys the stdlib logger allows on a record.

        ``message`` and ``args`` are reserved on ``LogRecord``; passing either
        through ``extra=`` raises rather than logging, which is a poor way to find
        out about an error.
        """
        return {"error": self.code, "reason": self.message, "details": self.details}


# ------------------------------------------------------------------- storage


class StoreError(SentinelError):
    """The event log could not do what was asked of it."""

    code = "store_error"


class ChainBroken(StoreError):
    """The log fails verification — a record was altered, removed or reordered."""

    code = "chain_broken"


class LogCorrupt(StoreError):
    """The log file cannot be read as far as it claims to go (e.g. a torn write)."""

    code = "log_corrupt"


class ConcurrencyConflict(StoreError):
    """Another writer moved the stream head between read and write."""

    code = "concurrency_conflict"


class SealMismatch(StoreError):
    """A seal does not match the chain it covers, or its MAC does not verify."""

    code = "seal_mismatch"


class SchemaTooNew(StoreError):
    """The database was written by a newer version of this tool."""

    code = "schema_too_new"


class CheckpointUnusable(StoreError):
    """A stored checkpoint cannot be trusted, so it was not used.

    Never fatal on its own: a checkpoint is only ever an optimisation, and the log it
    was taken from is still the ground truth. Callers that catch this fall back to a
    full replay and get the same answer, slower.
    """

    code = "checkpoint_unusable"


# ------------------------------------------------------------------ commands


class CommandRejected(SentinelError):
    """A command failed validation or an invariant; nothing was appended."""

    code = "command_rejected"


class ValidationFailed(CommandRejected):
    """A field on the command is missing, malformed or out of range."""

    code = "validation_failed"


class DuplicateTransaction(CommandRejected):
    """This ``txn_id`` is already on the log; identities derived from it would collide."""

    code = "duplicate_transaction"


class OutOfOrder(CommandRejected):
    """An observation arrived with a timestamp before one already recorded."""

    code = "out_of_order"


class UnknownCase(CommandRejected):
    """No such case in the queue projection."""

    code = "unknown_case"


class CaseAlreadyClosed(CommandRejected):
    """A case is dispositioned at most once."""

    code = "case_already_closed"


class NoHandler(CommandRejected):
    """No handler is registered for this command type — a wiring bug."""

    code = "no_handler"


# --------------------------------------------------------------------- query


class QueryError(SentinelError):
    """The read side could not answer."""

    code = "query_error"


class CaseNotFound(QueryError):
    """The case was never opened in this log."""

    code = "case_not_found"


class InvalidPath(SentinelError):
    """A path argument is not usable (missing, not a file, or not local)."""

    code = "invalid_path"


__all__ = [
    "CaseAlreadyClosed",
    "CaseNotFound",
    "ChainBroken",
    "CheckpointUnusable",
    "CommandRejected",
    "ConcurrencyConflict",
    "DuplicateTransaction",
    "InvalidPath",
    "LogCorrupt",
    "NoHandler",
    "OutOfOrder",
    "QueryError",
    "SchemaTooNew",
    "SealMismatch",
    "SentinelError",
    "StoreError",
    "UnknownCase",
    "ValidationFailed",
]
