"""Read-only queries the event log makes possible: audit trails, time travel and
counterfactual policy replay. Nothing in this package writes to the store.
"""
from .audit import TrailEntry, case_trail
from .counterfactual import (
    PolicyVariant,
    VariantResult,
    baseline_variant,
    replay_variants,
    standard_sweep,
    sweep_rows,
)
from .timetravel import KnowledgeSnapshot, knowledge_at_case, read_model_as_of, seq_of_case

__all__ = [
    "KnowledgeSnapshot",
    "PolicyVariant",
    "TrailEntry",
    "VariantResult",
    "baseline_variant",
    "case_trail",
    "knowledge_at_case",
    "read_model_as_of",
    "replay_variants",
    "seq_of_case",
    "standard_sweep",
    "sweep_rows",
]
