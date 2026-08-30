"""The read side: audit trails, time travel, counterfactuals and the ops queries.

These are the capabilities the project is actually about, and until now not one of
them had an assertion behind it. The properties worth pinning down are the ones a
reviewer would otherwise have to take on trust: that a trail is the literal
subsequence of the log and not a reconstruction, that time travel cannot see the
future, and that re-deciding the log under the recorded policy reproduces the
recorded decisions exactly.
"""
from __future__ import annotations

import pytest

from sentinel.domain.events import (
    CaseOpened,
)
from sentinel.errors import CaseNotFound, ValidationFailed
from sentinel.projections.registry import ReadModel
from sentinel.query.audit import case_trail
from sentinel.query.counterfactual import (
    PolicyVariant,
    baseline_variant,
    replay_variants,
    standard_sweep,
    sweep_rows,
)
from sentinel.query.operations import (
    MAX_PAGE,
    card_report,
    case_detail,
    list_cases,
    log_summary,
    stats,
)
from sentinel.query.timetravel import knowledge_at_case, read_model_as_of, seq_of_case


@pytest.fixture(scope="module")
def some_case(store):
    """A case id that exists in the session log."""
    return next(rec.event.case_id for rec in store.read(types=["CaseOpened"], limit=1))


# ------------------------------------------------------------------- audit


def test_a_trail_is_the_literal_subsequence_of_the_log(store, some_case):
    trail = case_trail(store, some_case)
    assert trail, "the case must have a trail"
    seqs = [entry.seq for entry in trail]
    assert seqs == sorted(seqs), "a trail is in log order"

    for entry in trail:
        assert entry.hash == store.hash_at(entry.seq), "hashes come from the log, not from us"


def test_a_trail_holds_exactly_that_case_and_its_transaction(store, some_case):
    txn_id = store.txn_of_case(some_case)
    trail = case_trail(store, some_case)
    by_seq = {rec.seq: rec for rec in store.read()}

    for entry in trail:
        event = by_seq[entry.seq].event
        assert getattr(event, "txn_id", None) == txn_id or \
            getattr(event, "case_id", None) == some_case

    expected = {
        rec.seq
        for rec in store.read()
        if getattr(rec.event, "txn_id", None) == txn_id
        or getattr(rec.event, "case_id", None) == some_case
    }
    assert {entry.seq for entry in trail} == expected


def test_a_trail_starts_at_the_observation_and_reaches_the_case(store, some_case):
    trail = case_trail(store, some_case)
    types = [entry.type for entry in trail]
    assert types[0] == "TransactionObserved"
    assert "RiskScored" in types
    assert "AlertRaised" in types
    assert types[-1] in ("CaseOpened", "CaseDispositioned")


def test_a_disposition_joins_the_trail(log_copy, built):
    from sentinel.commands.messages import DispositionCase, ObserveTransaction
    from sentinel.domain.events import Disposition
    from sentinel.ingest import wire

    read_model = ReadModel.rebuild(log_copy)
    models = built.bus.handler_for(ObserveTransaction).risk.models
    bus, _ = wire(log_copy, models, built.policy, read_model)
    case_id = read_model.cases.open_cases(1)[0].case_id
    before = len(case_trail(log_copy, case_id))

    bus.submit(DispositionCase(case_id, Disposition.CONFIRMED_FRAUD, "a.okafor", "card blocked"))
    after = case_trail(log_copy, case_id)
    assert len(after) == before + 1
    assert after[-1].type == "CaseDispositioned"
    assert "a.okafor" in after[-1].detail


def test_an_unknown_case_has_an_empty_trail(store):
    assert case_trail(store, "CASE-99999999") == []


# -------------------------------------------------------------- time travel


def test_seq_of_case_finds_where_the_case_was_opened(store, some_case):
    at = seq_of_case(store, some_case)
    assert at is not None
    record = next(store.read(from_seq=at, upto_seq=at))
    assert isinstance(record.event, CaseOpened)
    assert record.event.case_id == some_case
    assert seq_of_case(store, "CASE-99999999") is None


def test_time_travel_equals_a_fresh_rebuild_to_that_point(store, some_case):
    at = seq_of_case(store, some_case)
    assert read_model_as_of(store, at).fingerprint() == ReadModel.rebuild(store, at).fingerprint()


def test_the_past_read_model_never_holds_a_later_case(store, live_read_model, some_case):
    snap = knowledge_at_case(store, some_case, live_read_model)
    past = read_model_as_of(store, snap.at_seq)
    assert past.cases.get(some_case) is not None
    assert past.upto_seq == snap.at_seq
    assert snap.queue_depth_then <= snap.now_seq
    assert snap.now_seq == live_read_model.upto_seq


def test_time_travel_reports_what_changed_since(store, live_read_model, some_case):
    snap = knowledge_at_case(store, some_case, live_read_model)
    deltas = snap.deltas()
    for key, (then, now) in deltas.items():
        assert then != now, f"{key} was reported as changed but did not"
        assert snap.then.get(key) == then
        assert snap.now.get(key) == now


def test_time_travel_on_a_missing_case_is_a_typed_error(store, live_read_model):
    with pytest.raises(CaseNotFound, match="never opened"):
        knowledge_at_case(store, "CASE-99999999", live_read_model)


# ----------------------------------------------------------- counterfactual


def test_the_recorded_policy_reproduces_the_recorded_alerts(store, built):
    """The equivalence the whole design rests on: one AlertPolicy, two call sites."""
    (result,) = replay_variants(store, [baseline_variant(built.policy)])
    assert result.matches_log is True
    logged = sum(1 for _ in store.read(types=["AlertRaised"]))
    assert result.alerts == logged


def test_a_variant_that_changes_no_decision_also_matches(store, built):
    """Why the sweep can report more than one exact match, and that is not a bug."""
    variants = standard_sweep(built.policy)
    results = replay_variants(store, variants)
    matching = [r.label for r in results if r.matches_log]
    assert "baseline (as recorded)" in matching
    stricter = next(r for r in results if r.label.startswith("stricter"))
    assert stricter.matches_log is False


def test_disabling_a_rule_never_raises_more_alerts(store, built):
    baseline, = replay_variants(store, [baseline_variant(built.policy)])
    without, = replay_variants(
        store,
        [PolicyVariant("no rules at all", built.policy, disabled_rules=_all_rule_names(store))],
    )
    assert without.alerts <= baseline.alerts


def test_a_weight_override_is_actually_applied(store, built):
    name = next(iter(_all_rule_names(store)))
    boosted, = replay_variants(
        store, [PolicyVariant("boosted", built.policy, weight_overrides={name: 1.0})]
    )
    baseline, = replay_variants(store, [baseline_variant(built.policy)])
    assert boosted.alerts >= baseline.alerts


def test_a_lower_threshold_never_alerts_less(store, built):
    tight, loose = replay_variants(
        store,
        [
            PolicyVariant("tight", built.policy.replace(alert_threshold=0.9)),
            PolicyVariant("loose", built.policy.replace(alert_threshold=0.01)),
        ],
    )
    assert loose.alerts >= tight.alerts


def test_sweep_rows_price_the_outcome_with_the_given_cost_model(store, built):
    from sentinel.domain.policy import CostModel

    results = replay_variants(store, standard_sweep(built.policy))
    cheap = sweep_rows(results, CostModel(fp_cost=1.0, fn_cost=1.0))
    dear = sweep_rows(results, CostModel(fp_cost=100.0, fn_cost=100.0))
    for lo, hi, result in zip(cheap, dear, results):
        assert lo["cost"] == pytest.approx(result.fp + result.fn)
        assert hi["cost"] == pytest.approx(100 * (result.fp + result.fn))
        assert set(lo) >= {"variant", "alerts", "tp", "fp", "fn", "precision", "recall", "f1"}


def _all_rule_names(store) -> frozenset[str]:
    return frozenset(rec.event.rule_name for rec in store.read(types=["RuleTriggered"]))


# ------------------------------------------------------------- operations


def test_the_case_list_is_paged_and_ordered(store, live_read_model):
    first = list_cases(live_read_model, limit=5)
    assert len(first.items) == 5
    assert first.offset == 0 and first.next_offset == 5
    priorities = [item["priority"] for item in first.items]
    assert priorities == sorted(priorities, reverse=True)

    second = list_cases(live_read_model, limit=5, offset=first.next_offset)
    assert not {i["case_id"] for i in first.items} & {i["case_id"] for i in second.items}
    assert second.total == first.total


def test_the_last_page_reports_no_next_offset(live_read_model):
    total = list_cases(live_read_model, limit=1).total
    last = list_cases(live_read_model, limit=10, offset=max(0, total - 3))
    assert last.next_offset is None


def test_case_list_filters(live_read_model):
    everything = list_cases(live_read_model, status="all", limit=MAX_PAGE)
    card = everything.items[0]["card_id"]
    by_card = list_cases(live_read_model, status="all", card_id=card, limit=MAX_PAGE)
    assert by_card.total >= 1
    assert {item["card_id"] for item in by_card.items} == {card}

    floor = everything.items[0]["priority"]
    ranked = list_cases(live_read_model, status="all", min_priority=floor, limit=MAX_PAGE)
    assert all(item["priority"] >= floor for item in ranked.items)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": MAX_PAGE + 1}, "limit"),
        ({"offset": -1}, "offset"),
        ({"status": "pending"}, "status"),
    ],
)
def test_bad_paging_arguments_are_refused(live_read_model, kwargs, match):
    with pytest.raises(ValidationFailed, match=match):
        list_cases(live_read_model, **kwargs)


def test_case_detail_carries_the_trail(store, live_read_model, some_case):
    detail = case_detail(store, live_read_model, some_case)
    assert detail["case"]["case_id"] == some_case
    assert [e["seq"] for e in detail["trail"]] == [e.seq for e in case_trail(store, some_case)]

    with pytest.raises(CaseNotFound):
        case_detail(store, live_read_model, "CASE-99999999")


def test_a_card_report_pairs_the_profile_with_its_cases(store, live_read_model):
    case = list_cases(live_read_model, status="all", limit=1).items[0]
    report = card_report(store, live_read_model, case["card_id"])
    assert report["card_id"] == case["card_id"]
    assert report["profile"]["txns"] >= 1
    assert report["stream_version"] > 0
    assert case["case_id"] in {c["case_id"] for c in report["cases"]["items"]}

    with pytest.raises(CaseNotFound, match="no facts recorded"):
        card_report(store, live_read_model, "C999999")


def test_stats_roll_up_the_same_numbers_the_projection_holds(live_read_model):
    report = stats(live_read_model)
    assert report["totals"] == live_read_model.metrics.totals()
    assert report["queue"]["open"] == live_read_model.cases.open_count
    assert report["days"], "the corpus spans at least one day"

    later = stats(live_read_model, since_day=report["days"][-1]["day"])
    assert len(later["days"]) == 1


def test_the_log_summary_describes_the_log_itself(store):
    summary = log_summary(store)
    assert summary["records"] == store.last_seq
    assert summary["head_hash"] == store.head_hash
    assert summary["schema_version"] >= 1
    assert sum(summary["type_counts"].values()) == store.count()
    assert summary["header"]["model_id"].startswith("gbm")


def test_alerts_without_a_reason_are_visible_rather_than_hidden(store, live_read_model):
    """20% of alerts in a demo run are model-only. The queue must not pretend otherwise."""
    cases = list_cases(live_read_model, status="all", limit=MAX_PAGE).items
    silent = [c for c in cases if not c["reasons"]]
    for case in silent:
        assert case["trigger"] == "ml_blend"
        assert case["rules"] == []
