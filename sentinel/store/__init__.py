"""Persistence: the append-only, hash-chained JSONL event log and its codec."""
from .codec import GENESIS_HASH, ChainBroken, Recorded, canonical_json, chain_hash, decode, encode
from .jsonl_store import EventStore

__all__ = [
    "GENESIS_HASH",
    "ChainBroken",
    "EventStore",
    "Recorded",
    "canonical_json",
    "chain_hash",
    "decode",
    "encode",
]
