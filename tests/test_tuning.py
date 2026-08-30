"""The operating point must be tuned on the score the policy compares.

The bug this guards against was silent and expensive: the threshold was tuned against
the model probability, but the policy compares ``(1-rw)*ml + rw*rules``. With
``rule_weight = 0.35`` a transaction no rule fired on blends to at most 0.65, so any
tuned threshold above that made a whole class of transactions unalertable however
certain the model was.
"""
from __future__ import annotations

import numpy as np
import pytest

from sentinel.detection.ensemble import TRAIN_FRACTION, temporal_split
from sentinel.detection.tuning import (
    OperatingPoint,
    policy_for,
    rule_scores,
    tune_alert_threshold,
)
from sentinel.domain.policy import CostModel

RULE_WEIGHT = 0.35
FLOOR = 0.85


def tune(ml, rules, labels, costs=CostModel()):
    return tune_alert_threshold(
        np.array(ml, dtype=float),
        np.array(rules, dtype=float),
        np.array(labels, dtype=int),
        rule_weight=RULE_WEIGHT,
        rules_only_floor=FLOOR,
        costs=costs,
    )


def test_the_chosen_threshold_is_reachable_by_the_blend():
    """A model-certain fraud with no rule fire must still be alertable."""
    ml = [1.0] * 20 + [0.02] * 200
    rules = [0.0] * 220
    labels = [1] * 20 + [0] * 200

    point = tune(ml, rules, labels)
    policy = policy_for(point, rule_weight=RULE_WEIGHT, rules_only_floor=FLOOR)
    assert policy.alert_threshold <= (1.0 - RULE_WEIGHT), (
        "a cut above the no-rules ceiling would make these transactions unalertable"
    )
    assert policy.should_alert(1.0, 0.0) is True
    assert point.recall == 1.0


def test_the_policy_built_from_the_point_reproduces_its_counts():
    ml = list(np.linspace(0.0, 1.0, 300))
    rules = [0.0] * 300
    labels = [1 if value > 0.8 else 0 for value in ml]

    point = tune(ml, rules, labels)
    policy = policy_for(point, rule_weight=RULE_WEIGHT, rules_only_floor=FLOOR)
    fired = [policy.should_alert(m, r) for m, r in zip(ml, rules, strict=True)]
    tp = sum(1 for f, y in zip(fired, labels, strict=True) if f and y == 1)
    fp = sum(1 for f, y in zip(fired, labels, strict=True) if f and y == 0)
    fn = sum(1 for f, y in zip(fired, labels, strict=True) if not f and y == 1)
    assert (tp, fp, fn) == (point.tp, point.fp, point.fn)


def test_an_expensive_false_negative_buys_recall():
    ml = list(np.linspace(0.0, 1.0, 200))
    rules = [0.0] * 200
    labels = [1 if value > 0.6 else 0 for value in ml]

    cheap_misses = tune(ml, rules, labels, CostModel(fp_cost=100.0, fn_cost=1.0))
    dear_misses = tune(ml, rules, labels, CostModel(fp_cost=1.0, fn_cost=1000.0))
    assert dear_misses.threshold <= cheap_misses.threshold
    assert dear_misses.recall >= cheap_misses.recall


def test_the_rules_only_floor_is_part_of_the_outcome():
    """A rule at the floor alerts whatever the cut is, so tuning must count it."""
    ml = [0.0] * 50
    rules = [0.9] * 10 + [0.0] * 40
    labels = [1] * 10 + [0] * 40

    point = tune(ml, rules, labels)
    assert point.tp == 10 and point.fn == 0


def test_unlabelled_rows_are_ignored():
    ml = [0.9, 0.1, 0.5, 0.5]
    rules = [0.0, 0.0, 0.0, 0.0]
    labels = [1, 0, -1, -1]
    point = tune(ml, rules, labels)
    assert point.tp + point.fp + point.fn <= 2


def test_tuning_without_a_label_is_refused():
    with pytest.raises(ValueError, match="labelled rows"):
        tune([0.5], [0.0], [-1])


def test_the_operating_point_reports_its_own_arithmetic():
    point = OperatingPoint(threshold=0.4, tp=8, fp=2, fn=2, cost=123.0)
    assert point.precision == 0.8
    assert point.recall == 0.8
    payload = point.as_dict()
    assert payload["threshold"] == 0.4
    assert payload["blended_recall"] == 0.8


def test_the_threshold_is_never_zero():
    """The policy requires a positive cut; alerting on everything is not a cut."""
    point = tune([0.0] * 10, [0.0] * 10, [1] * 10)
    assert point.threshold > 0.0


# ------------------------------------------------------------------- wiring


def test_the_split_is_chronological_and_shared(corpus):
    from sentinel.detection.features import build_feature_frame

    feat = build_feature_frame(corpus.head(2000))
    train, test = temporal_split(feat)
    assert len(train) == int(len(feat) * TRAIN_FRACTION)
    assert train["ts"].max() <= test["ts"].min()
    assert len(train) + len(test) == len(feat)


def test_rule_scores_use_the_live_rule_book(corpus):
    from sentinel.detection.features import build_feature_frame
    from sentinel.detection.rules import evaluate

    feat = build_feature_frame(corpus.head(500))
    scored = rule_scores(feat)
    assert len(scored) == len(feat)
    assert scored.max() > 0.0, "some rule must fire somewhere in 500 transactions"
    by_hand = [evaluate(row)[0] for row in feat.to_dict("records")]
    assert list(scored) == by_hand


def test_the_built_log_alerts_on_model_certain_fraud(built):
    """End to end: the recorded policy must be reachable without a rule fire."""
    policy = built.policy
    assert policy.alert_threshold <= (1.0 - policy.rule_weight)
    assert policy.should_alert(1.0, 0.0) is True


def test_the_recorded_policy_is_not_dominated_by_muting_the_rules(store, built):
    """The demo used to print that turning the rules off was 100x cheaper."""
    from sentinel.query.counterfactual import PolicyVariant, baseline_variant, replay_variants

    baseline, muted = replay_variants(
        store,
        [
            baseline_variant(built.policy),
            PolicyVariant(
                "model only", built.policy.replace(rule_weight=0.0, rules_only_floor=2.0)
            ),
        ],
    )
    costs = CostModel()
    assert baseline.cost(costs) <= muted.cost(costs) * 3, (
        "the recorded operating point should be in the same league as muting the rules"
    )
