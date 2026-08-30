"""Stable identity for a command, which is what makes retries safe.

A command may be delivered twice — a retried batch, a resumed ingest, an analyst
double-tapping a key. Without an identity the second delivery appends a second set of
events under the same derived ``ALERT-``/``CASE-`` ids, and the queue projection
quietly overwrites the first case.

The id is derived from the command's own content unless the caller supplies one, so
two identical submissions are the same command by construction and a replay of the
same corpus produces the same ids on every machine. A random uuid would defeat the
whole point: the retry would look like a new instruction.
"""
from __future__ import annotations

import enum
import hashlib
from dataclasses import fields
from typing import Any

from ..store.codec import canonical_json


def command_key(command: Any) -> str:
    """Return the command's idempotency key.

    Uses ``command.command_id`` when the caller set one, otherwise a digest over the
    command type and every field.
    """
    explicit = getattr(command, "command_id", "")
    if explicit:
        return str(explicit)
    payload: dict[str, Any] = {"__type__": type(command).__name__}
    for field in fields(command):
        if field.name == "command_id":
            continue
        value = getattr(command, field.name)
        payload[field.name] = value.value if isinstance(value, enum.Enum) else value
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return digest[:32]


__all__ = ["command_key"]
