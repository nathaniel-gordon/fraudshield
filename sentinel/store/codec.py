"""Serialisation and the tamper-evident hash chain.

The record contract is defined in exactly one place, here, and is the same whether a
record lives in a SQLite row or in a line of an exported JSONL archive:

    {"seq": 1, "type": "TransactionObserved", "prev": "<64 hex>", "hash": "<64 hex>",
     "data": { ... }}

``hash`` chains each record to its predecessor, so any in-place edit, deletion or
reordering is caught by a single forward walk (:meth:`EventStore.verify`).

What the chain does **not** do, and this is worth being blunt about: it does not stop
somebody who can write the file from editing a payload and recomputing every
downstream hash. Detecting that needs an anchor the attacker cannot recompute — see
:mod:`sentinel.store.seal`, which MACs the head with a key held outside the database.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from ..domain.events import EVENT_TYPES, DomainEvent
from ..errors import ChainBroken, LogCorrupt

#: The chain's anchor. Record 1 has this as its predecessor hash.
GENESIS_HASH = "0" * 64


def canonical_json(payload: Any) -> str:
    """Stable JSON text: sorted keys, no incidental whitespace.

    Hashes and projection fingerprints both depend on byte-stability, so every
    place that turns a structure into text funnels through here.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chain_hash_text(prev_hash: str, seq: int, type_name: str, data_json: str) -> str:
    """Chain hash computed from already-canonical payload text.

    The storage layer keeps the canonical text it hashed, so verification can hash
    exactly those bytes rather than a re-serialised structure.
    """
    material = f"{prev_hash}|{seq}|{type_name}|{data_json}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def chain_hash(prev_hash: str, seq: int, type_name: str, data: dict[str, Any]) -> str:
    """Hash of this record bound to its position and its predecessor."""
    return chain_hash_text(prev_hash, seq, type_name, canonical_json(data))


@dataclass(frozen=True, slots=True)
class Recorded:
    """An event as it exists in the log: the fact plus its position and chain links.

    Projections receive ``Recorded`` rather than a bare event so they can expose
    the sequence number an analyst needs for time travel and audit.
    """

    seq: int
    event: DomainEvent
    prev_hash: str
    hash: str

    @property
    def type(self) -> str:
        return self.event.type


def encode(seq: int, event: DomainEvent, prev_hash: str) -> tuple[str, Recorded]:
    """Return the JSONL line and the resulting :class:`Recorded` for one event."""
    data = event.to_data()
    digest = chain_hash(prev_hash, seq, event.type, data)
    line = canonical_json(
        {"seq": seq, "type": event.type, "prev": prev_hash, "hash": digest, "data": data}
    )
    return line, Recorded(seq=seq, event=event, prev_hash=prev_hash, hash=digest)


def event_from_data(type_name: str, data: dict[str, Any], *, seq: int | None = None) -> DomainEvent:
    """Rehydrate one event from its payload.

    Raises:
        ChainBroken: the type name is not in this build's vocabulary, so the record
            cannot be folded and pretending otherwise would silently drop history.
    """
    cls = EVENT_TYPES.get(type_name)
    if cls is None:
        raise ChainBroken(f"unknown event type {type_name!r}", seq=seq, type=type_name)
    return cls.from_data(_coerce(cls, data))


def decode(line: str) -> Recorded:
    """Parse one JSONL line back into a :class:`Recorded`, checking its own hash.

    Raises:
        LogCorrupt: the line is not complete, well-formed JSON (a torn write).
        ChainBroken: the type is unknown, or the payload does not hash to the value
            written on the line.
    """
    try:
        obj = json.loads(line)
        type_name = obj["type"]
        seq = int(obj["seq"])
        prev, digest, data = obj["prev"], obj["hash"], obj["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise LogCorrupt(f"line is not a complete record: {exc}") from exc
    expected = chain_hash(prev, seq, type_name, data)
    if expected != digest:
        raise ChainBroken(
            f"record {seq} ({type_name}) fails its own hash — payload was altered",
            seq=seq,
            type=type_name,
        )
    return Recorded(
        seq=seq, event=event_from_data(type_name, data, seq=seq), prev_hash=prev, hash=digest
    )


def _mentions_tuple(annotation: Any) -> bool:
    """True when a resolved annotation is a tuple, or a union containing one."""
    origin = get_origin(annotation)
    if origin is tuple:
        return True
    if origin in (Union, UnionType):
        return any(_mentions_tuple(arg) for arg in get_args(annotation))
    return False


#: Memoised per event class. ``get_type_hints`` is not cheap and the vocabulary is
#: fixed at import time, so resolving it once per class is the whole optimisation.
_TUPLE_FIELDS: dict[str, frozenset[str]] = {}


def _tuple_fields(cls: type[DomainEvent]) -> frozenset[str]:
    """Field names whose declared type is (or includes) a tuple.

    Resolved from real type objects rather than by sniffing the annotation string,
    so a field annotated ``Sequence[str]`` is not silently mishandled.
    """
    cached = _TUPLE_FIELDS.get(cls.__name__)
    if cached is not None:
        return cached
    hints = get_type_hints(cls)
    names = frozenset(
        f.name
        for f in dataclass_fields(cls)  # type: ignore[arg-type]
        if _mentions_tuple(hints.get(f.name))
    )
    _TUPLE_FIELDS[cls.__name__] = names
    return names


def _coerce(cls: type[DomainEvent], data: dict[str, Any]) -> dict[str, Any]:
    """Restore tuple-typed fields that JSON flattened into lists."""
    names = _tuple_fields(cls)
    if not names:
        return data
    out = dict(data)
    for name in names:
        if isinstance(out.get(name), list):
            out[name] = tuple(out[name])
    return out


__all__ = [
    "GENESIS_HASH",
    "ChainBroken",
    "Recorded",
    "canonical_json",
    "chain_hash",
    "chain_hash_text",
    "decode",
    "encode",
    "event_from_data",
]
