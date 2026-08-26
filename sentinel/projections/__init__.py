"""Query side: read models built purely by folding the event log.

Every class here is a fold and nothing else — no IO, no model calls, no clock. That
discipline is what makes ``ReadModel.rebuild`` and as-of reconstruction possible.
"""
from .base import Projection
from .card_profile import CardProfileProjection
from .case_queue import CaseQueueProjection, CaseView
from .daily_metrics import DailyMetricsProjection
from .registry import ReadModel, default_projections
from .rule_performance import RulePerformanceProjection

__all__ = [
    "CardProfileProjection",
    "CaseQueueProjection",
    "CaseView",
    "DailyMetricsProjection",
    "Projection",
    "ReadModel",
    "RulePerformanceProjection",
    "default_projections",
]
