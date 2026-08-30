"""Persistence: the append-only, hash-chained SQLite event log and its codec."""
from ..errors import ChainBroken, ConcurrencyConflict, LogCorrupt
from .codec import (
    GENESIS_HASH,
    Recorded,
    canonical_json,
    chain_hash,
    chain_hash_text,
    decode,
    encode,
)
from .jsonl import ImportReport, export_jsonl, import_jsonl, read_jsonl
from .schema import SCHEMA_VERSION, migrate
from .sqlite_store import CommandGroup, DuplicateCommand, EventStore

__all__ = [
    "GENESIS_HASH",
    "SCHEMA_VERSION",
    "ChainBroken",
    "CommandGroup",
    "ConcurrencyConflict",
    "DuplicateCommand",
    "EventStore",
    "ImportReport",
    "LogCorrupt",
    "Recorded",
    "canonical_json",
    "chain_hash",
    "chain_hash_text",
    "decode",
    "encode",
    "export_jsonl",
    "import_jsonl",
    "migrate",
    "read_jsonl",
]
