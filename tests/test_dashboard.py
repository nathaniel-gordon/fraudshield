"""The analyst console, driven by keystrokes and asserted on exact frame content.

``render()`` is a pure function of state returning a list of strings, which is the
whole reason a terminal UI can be tested at all. Colour is disabled throughout, so the
frames here are the same characters a legacy Windows code page would get.

The interesting assertions are not about pixels. They are that a keystroke appends
exactly one event, that the queue shrinks because a projection folded that event
rather than because the UI edited a list, and that a cached view of a case is dropped
the moment that case's facts change.
"""
from __future__ import annotations

import io

import pytest

from sentinel.commands.messages import ObserveTransaction
from sentinel.ingest import wire
from sentinel.projections.registry import ReadModel
from sentinel.ui.ansi import Palette, strip_ansi
from sentinel.ui.dashboard import (
    AUDIT,
    COUNTERFACTUAL,
    HELP,
    METRICS,
    QUEUE,
    RULES,
    TIMETRAVEL,
    Dashboard,
)
from sentinel.ui.runner import DEMO_SCRIPT, run_scripted


@pytest.fixture
def console(log_copy, built):
    """A dashboard over a private copy of the session log, colour off."""
    read_model = ReadModel.rebuild(log_copy)
    models = built.bus.handler_for(ObserveTransaction).risk.models
    bus, _ = wire(log_copy, models, built.policy, read_model)
    return Dashboard(
        log_copy, read_model, bus, built.policy,
        width=100, height=26, palette=Palette(False), analyst="a.okafor",
    )


def body(dashboard: Dashboard) -> str:
    return "\n".join(dashboard.render())


# ------------------------------------------------------------------ frames


def test_a_frame_is_a_rectangle_of_the_requested_size(console):
    frame = console.render()
    assert len(frame) == console.height
    assert {len(strip_ansi(line)) for line in frame} == {console.width}


def test_the_frame_is_a_pure_function_of_state(console):
    assert console.render() == console.render()


def test_colour_changes_only_the_escape_codes(console):
    plain = console.render()
    console.set_palette(Palette(True))
    coloured = console.render()
    assert [strip_ansi(line) for line in coloured] == plain
    assert any("\x1b[" in line for line in coloured)


def test_the_header_reports_the_log_and_the_queue(console):
    frame = console.render()
    assert f"{console.store.last_seq:,} events" in frame[0]
    assert console.store.head_hash[:8] in frame[0]
    assert f"open  {console.read_model.cases.open_count}" in frame[1]
    assert f"closed   {console.read_model.cases.closed_count}" in frame[1]


def test_the_queue_view_lists_the_highest_priority_cases_first(console):
    text = body(console)
    assert f"─ {QUEUE} ─" in text
    ranked = console.queue()
    for case in ranked[:5]:
        assert case.case_id in text
    priorities = [c.priority for c in ranked]
    assert priorities == sorted(priorities, reverse=True)


@pytest.mark.parametrize(
    ("key", "view"),
    [
        ("a", AUDIT),
        ("t", TIMETRAVEL),
        ("m", METRICS),
        ("r", RULES),
        ("c", COUNTERFACTUAL),
        ("?", HELP),
    ],
)
def test_every_view_renders_and_names_itself(console, key, view):
    assert console.handle(key) is True
    assert console.state.view == view
    assert f"─ {view} ─" in body(console)
    assert console.handle("1") is True
    assert console.state.view == QUEUE


def test_the_audit_view_shows_the_facts_behind_the_selected_case(console):
    case = console.selected()
    console.handle("a")
    text = body(console)
    assert case.case_id in text
    assert "TransactionObserved" in text
    assert "RiskScored" in text


def test_the_time_travel_view_names_the_prefix_it_rebuilt(console):
    case = console.selected()
    console.handle("t")
    text = body(console)
    assert case.case_id in text
    assert "Projections rebuilt over records 1.." in text


def test_the_counterfactual_view_marks_what_reproduces_the_log(console):
    console.handle("c")
    text = body(console)
    assert "baseline (as recorded)" in text
    assert "model only (rules muted)" in text


def test_an_unbound_key_says_so_without_changing_the_view(console):
    console.handle("z")
    assert console.state.view == QUEUE
    assert "unbound key 'z'" in console.state.status


def test_q_ends_the_session(console):
    assert console.handle("q") is False


# ----------------------------------------------------------------- cursor


def test_the_cursor_clamps_at_both_ends(console):
    for _ in range(len(console.queue()) + 20):
        console.handle("j")
    assert console.state.cursor == len(console.queue()) - 1
    for _ in range(len(console.queue()) + 20):
        console.handle("k")
    assert console.state.cursor == 0

    console.handle("END")
    assert console.state.cursor == len(console.queue()) - 1
    console.handle("HOME")
    assert console.state.cursor == 0
    console.handle("PGDN")
    assert 0 < console.state.cursor < len(console.queue())
    console.handle("PGUP")
    assert console.state.cursor == 0


def test_an_empty_queue_renders_and_dispositions_nothing(console):
    while console.queue():
        console.handle("f")
    assert console.selected() is None
    assert console.state.cursor == 0
    console.handle("f")
    assert "queue is empty" in console.state.status
    assert len(console.render()) == console.height


# ------------------------------------------------------------ dispositions


def test_a_keystroke_appends_exactly_one_event_and_shrinks_the_queue(console):
    before_seq = console.store.last_seq
    before_depth = len(console.queue())
    case = console.selected()

    console.handle("f")

    assert console.store.last_seq == before_seq + 1
    assert len(console.queue()) == before_depth - 1
    assert console.state.dispositions == 1
    appended = next(console.store.read(from_seq=before_seq + 1))
    assert appended.type == "CaseDispositioned"
    assert appended.event.case_id == case.case_id
    assert appended.event.analyst == "a.okafor"
    assert console.store.verify() == console.store.last_seq


@pytest.mark.parametrize(
    ("key", "outcome"), [("f", "confirmed_fraud"), ("g", "genuine"), ("x", "escalated")]
)
def test_each_disposition_key_records_its_own_outcome(console, key, outcome):
    console.handle(key)
    appended = next(console.store.read(from_seq=console.store.last_seq))
    assert appended.event.disposition == outcome


def test_the_queue_shrinks_because_a_projection_folded_the_event(console):
    """Not because the UI removed a row: a fresh rebuild must agree."""
    console.handle("f")
    rebuilt = ReadModel.rebuild(console.store)
    assert rebuilt.fingerprint() == console.read_model.fingerprint()


def test_a_cached_audit_trail_is_dropped_when_the_case_changes(console):
    """The bug this test exists for: press a, disposition, press a — stale trail."""
    case = console.selected()
    console.handle("a")
    body(console)  # the cache is filled while rendering, not while handling
    cached = console.state.caches["trail"][case.case_id]
    assert "CaseDispositioned" not in {entry.type for entry in cached}

    console.handle("1")
    console.state.cursor = console.queue().index(case)
    console.handle("f")
    assert case.case_id not in console.state.caches.get("trail", {})

    console.state.audit_case = case.case_id
    console.state.view = AUDIT
    assert "CaseDispositioned" in body(console)


def test_a_cached_time_travel_snapshot_is_dropped_too(console):
    case = console.selected()
    console.handle("t")
    body(console)
    assert case.case_id in console.state.caches["travel"]
    console.handle("1")
    console.state.cursor = console.queue().index(case)
    console.handle("f")
    assert case.case_id not in console.state.caches.get("travel", {})


def test_the_counterfactual_cache_is_dropped_after_a_disposition(console):
    console.handle("c")
    body(console)
    assert console.state.caches["counterfactual"]
    console.handle("1")
    console.handle("f")
    assert "counterfactual" not in console.state.caches


# ------------------------------------------------------------------ runner


def test_the_scripted_runner_terminates_and_returns_every_frame(console):
    out = io.StringIO()
    frames = run_scripted(console, DEMO_SCRIPT, out=out, show_frames=[0])
    assert len(frames) == len(DEMO_SCRIPT)  # the trailing "q" stops before rendering
    assert all(len(frame) == console.height for frame in frames)
    assert console.state.dispositions == 4
    assert "frame 1/" in out.getvalue()


def test_the_scripted_runner_can_render_silently(console):
    frames = run_scripted(console, ("j", "a", "1", "q"), out=None)
    assert len(frames) == 4


def test_a_script_of_dispositions_leaves_a_verifiable_log(console):
    run_scripted(console, DEMO_SCRIPT, out=None)
    assert console.store.verify() == console.store.last_seq
    assert ReadModel.rebuild(console.store).fingerprint() == console.read_model.fingerprint()
