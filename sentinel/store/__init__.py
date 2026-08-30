"""Persistence: the append-only, hash-chained SQLite event log, its codec and its seal."""
from ..errors import ChainBroken, ConcurrencyConflict, LogCorrupt, SealMismatch
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
from .seal import Seal, SealCheck, seal, seals, verify_seals
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
    "Seal",
    "SealCheck",
    "SealMismatch",
    "canonical_json",
    "chain_hash",
    "chain_hash_text",
    "decode",
    "encode",
    "export_jsonl",
    "import_jsonl",
    "migrate",
    "read_jsonl",
    "seal",
    "seals",
    "verify_seals",
]
