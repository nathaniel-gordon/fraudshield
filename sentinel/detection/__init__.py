"""Detection science: streaming features, the ensemble, the rule book, and the engine
that binds them. This subpackage is model machinery only — it knows nothing about
events, stores or projections, and the write side is its only caller.
"""
from .ensemble import FraudModels
from .features import FEATURES, FeatureStream, build_feature_frame
from .risk_engine import Assessment, RiskEngine
from .rules import RuleFire, evaluate, rule_book
from .synthetic import generate_transactions

__all__ = [
    "FEATURES",
    "Assessment",
    "FeatureStream",
    "FraudModels",
    "RiskEngine",
    "RuleFire",
    "build_feature_frame",
    "evaluate",
    "generate_transactions",
    "rule_book",
]
