"""Serialisation and the tamper-evident hash chain.

One JSON object per line. The line format is the durable contract of this system,
so it is defined in exactly one place:

    {"seq": 1, "type": "TransactionObserved", "prev": "<64 hex>", "hash": "<64 hex>",
     "data": { ... }}

``hash`` chains each record to its predecessor. Fraud evidence is only worth
anything if you can prove nobody edited it after the fact, and a chain makes any
in-place edit, deletion or reordering detectable by a single forward walk.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from typing import Any

from ..domain.events import EVENT_TYPES, DomainEvent

#: The chain's anchor. Record 1 has this as its predecessor hash.
GENESIS_HASH = "0" * 64


def canonical_json(payload: Any) -> str:
    """Stable JSON text: sorted keys, no incidental whitespace.

    Hashes and projection fingerprints both depend on byte-stability, so every
    place that turns a structure into text funnels through here.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chain_hash(prev_hash: str, seq: int, type_name: str, data: dict[str, Any]) -> str:
    """Hash of this record bound to its position and its predecessor."""
    material = f"{prev_hash}|{seq}|{type_name}|{canonical_json(data)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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


class ChainBroken(Exception):
    """Raised when the log fails verification — i.e. the evidence was altered."""


def encode(seq: int, event: DomainEvent, prev_hash: str) -> tuple[str, Recorded]:
    """Return the JSONL line and the resulting :class:`Recorded` for one event."""
    data = event.to_data()
    digest = chain_hash(prev_hash, seq, event.type, data)
    line = canonical_json(
        {"seq": seq, "type": event.type, "prev": prev_hash, "hash": digest, "data": data}
    )
    return line, Recorded(seq=seq, event=event, prev_hash=prev_hash, hash=digest)


def decode(line: str) -> Recorded:
    """Parse one JSONL line back into a :class:`Recorded`.

    Raises:
        ChainBroken: if the type is unknown or the payload does not hash to the
            value written on the line.
    """
    obj = json.loads(line)
    type_name = obj["type"]
    cls = EVENT_TYPES.get(type_name)
    if cls is None:
        raise ChainBroken(f"unknown event type {type_name!r} at seq {obj.get('seq')}")
    data = obj["data"]
    expected = chain_hash(obj["prev"], obj["seq"], type_name, data)
    if expected != obj["hash"]:
        raise ChainBroken(
            f"record {obj['seq']} ({type_name}) fails its own hash — payload was altered"
        )
    return Recorded(
        seq=int(obj["seq"]),
        event=cls.from_data(_coerce(cls, data)),
        prev_hash=obj["prev"],
        hash=obj["hash"],
    )


def _coerce(cls: type[DomainEvent], data: dict[str, Any]) -> dict[str, Any]:
    """Restore tuple-typed fields that JSON flattened into lists.

    Only ``DetectionPolicyAdopted.features`` needs this today, but doing it by
    annotation keeps the codec honest as the vocabulary grows.
    """
    out = dict(data)
    for field in dataclass_fields(cls):  # type: ignore[arg-type]
        if "tuple" in str(field.type) and isinstance(out.get(field.name), list):
            out[field.name] = tuple(out[field.name])
    return out
