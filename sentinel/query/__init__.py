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
from .operations import Page, card_report, case_detail, list_cases, log_summary, stats
from .timetravel import KnowledgeSnapshot, knowledge_at_case, read_model_as_of, seq_of_case

__all__ = [
    "KnowledgeSnapshot",
    "Page",
    "PolicyVariant",
    "TrailEntry",
    "VariantResult",
    "baseline_variant",
    "card_report",
    "case_detail",
    "case_trail",
    "knowledge_at_case",
    "list_cases",
    "log_summary",
    "read_model_as_of",
    "replay_variants",
    "seq_of_case",
    "standard_sweep",
    "stats",
    "sweep_rows",
]
