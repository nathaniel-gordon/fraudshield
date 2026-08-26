"""The analyst console.

Rendering is a *pure function of state*: :meth:`Dashboard.render` returns a list of
strings and touches neither stdout nor the terminal. That is what makes a TUI testable
— the suite asserts on exact frame content with colour disabled, and the interactive
and scripted runners differ only in where the keystrokes come from.

Dispositioning a case from here submits a ``DispositionCase`` command, which appends a
``CaseDispositioned`` event. The queue the analyst is looking at then updates because
the projection folded that event — not because the UI edited a list. A keystroke here
is therefore a permanent, replayable, auditable fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..commands.bus import CommandBus
from ..commands.handlers import CommandRejected
from ..commands.messages import DispositionCase
from ..domain.events import Disposition
from ..domain.policy import AlertPolicy
from ..projections.case_queue import CaseView
from ..projections.registry import ReadModel
from ..query.audit import case_trail
from ..query.counterfactual import replay_variants, standard_sweep, sweep_rows
from ..query.timetravel import knowledge_at_case
from ..store.jsonl_store import EventStore
from .ansi import (
    BOLD,
    DIM,
    FG_BRIGHT_CYAN,
    FG_BRIGHT_GREEN,
    FG_BRIGHT_RED,
    FG_BRIGHT_YELLOW,
    FG_GREY,
    FG_MAGENTA,
    REVERSE,
    Box,
    Palette,
    columns,
    fit,
)

QUEUE, AUDIT, TIMETRAVEL, METRICS, RULES, COUNTERFACTUAL, HELP = (
    "QUEUE",
    "AUDIT",
    "TIME-TRAVEL",
    "METRICS",
    "RULE PERFORMANCE",
    "COUNTERFACTUAL",
    "HELP",
)

_DISPOSITION_KEYS = {
    "f": (Disposition.CONFIRMED_FRAUD, "confirmed fraud — card blocked"),
    "g": (Disposition.GENUINE, "genuine — customer contacted, spend verified"),
    "x": (Disposition.ESCALATED, "escalated to the investigations team"),
}

_HELP_LINES = [
    "NAVIGATION",
    "  j / down     move the queue cursor down",
    "  k / up       move the queue cursor up",
    "  pgdn / pgup  move a page at a time",
    "  home / end   jump to the top / bottom of the queue",
    "",
    "DISPOSITIONING  (each keystroke appends a CaseDispositioned event to the log)",
    "  f            confirm fraud",
    "  g            mark genuine (false alert)",
    "  x            escalate to investigations",
    "",
    "VIEWS",
    "  a / enter    audit trail — the exact ordered events behind the selected case",
    "  t            time travel — the card profile as of the moment the case opened",
    "  m            daily metrics projection",
    "  r            rule performance projection",
    "  c            counterfactual policy replay (read-only; production untouched)",
    "  1 / esc      back to the case queue",
    "  ?            this screen",
    "  q            quit",
]


@dataclass(slots=True)
class DashboardState:
    """Everything the renderer needs; kept separate so frames stay reproducible."""

    view: str = QUEUE
    cursor: int = 0
    status: str = "ready"
    analyst: str = "analyst"
    audit_case: str | None = None
    travel_case: str | None = None
    dispositions: int = 0
    caches: dict[str, Any] = field(default_factory=dict)


class Dashboard:
    """Renders the read model and turns keystrokes into commands."""

    def __init__(
        self,
        store: EventStore,
        read_model: ReadModel,
        bus: CommandBus,
        policy: AlertPolicy,
        *,
        width: int = 112,
        height: int = 30,
        palette: Palette | None = None,
        analyst: str = "analyst",
    ) -> None:
        self.store = store
        self.read_model = read_model
        self.bus = bus
        self.policy = policy
        self.width = width
        self.height = height
        self.palette = palette or Palette(False)
        self.state = DashboardState(analyst=analyst)
        self.box = Box(width, self.palette)

    def set_palette(self, palette: Palette) -> None:
        """Swap the colour palette, keeping the box chrome in sync.

        The live runner decides colour only once it knows what stdout is; the
        dashboard is constructed before that.
        """
        self.palette = palette
        self.box = Box(self.width, palette)

    # ------------------------------------------------------------- selection

    def visible_rows(self) -> int:
        """How many queue rows fit between the chrome and the detail panel."""
        return max(4, self.height - 14)

    def queue(self) -> list[CaseView]:
        return self.read_model.cases.open_cases()

    def selected(self) -> CaseView | None:
        cases = self.queue()
        if not cases:
            return None
        self.state.cursor = max(0, min(self.state.cursor, len(cases) - 1))
        return cases[self.state.cursor]

    # ---------------------------------------------------------------- input

    def handle(self, key: str) -> bool:
        """Apply one keystroke. Returns ``False`` when the analyst asked to quit."""
        st = self.state
        if key == "q":
            return False
        if key in ("j", "DOWN"):
            st.cursor += 1
            self.selected()
        elif key in ("k", "UP"):
            st.cursor = max(0, st.cursor - 1)
        elif key == "PGDN":
            st.cursor += self.visible_rows()
            self.selected()
        elif key == "PGUP":
            st.cursor = max(0, st.cursor - self.visible_rows())
        elif key == "HOME":
            st.cursor = 0
        elif key == "END":
            st.cursor = max(0, len(self.queue()) - 1)
        elif key in _DISPOSITION_KEYS:
            self._disposition(*_DISPOSITION_KEYS[key])
        elif key in ("a", "ENTER"):
            case = self.selected()
            st.audit_case = case.case_id if case else None
            st.view = AUDIT
            st.status = f"audit trail for {st.audit_case}" if case else "queue is empty"
        elif key == "t":
            case = self.selected()
            st.travel_case = case.case_id if case else None
            st.view = TIMETRAVEL
            st.status = (
                f"rebuilt projections as of the moment {st.travel_case} opened"
                if case
                else "queue is empty"
            )
        elif key == "m":
            st.view, st.status = METRICS, "daily metrics projection"
        elif key == "r":
            st.view, st.status = RULES, "rule performance projection"
        elif key == "c":
            st.view, st.status = COUNTERFACTUAL, "counterfactual replay — production untouched"
        elif key == "?":
            st.view, st.status = HELP, "key bindings"
        elif key in ("1", "ESC"):
            st.view, st.status = QUEUE, "case queue"
        elif key:
            st.status = f"unbound key {key!r} — press ? for help"
        return True

    def _disposition(self, outcome: Disposition, note: str) -> None:
        case = self.selected()
        if case is None:
            self.state.status = "nothing to disposition — the queue is empty"
            return
        try:
            self.bus.submit(
                DispositionCase(
                    case_id=case.case_id,
                    disposition=outcome,
                    analyst=self.state.analyst,
                    note=note,
                )
            )
        except CommandRejected as exc:
            self.state.status = f"rejected: {exc}"
            return
        self.state.dispositions += 1
        self.state.caches.pop("counterfactual", None)
        self.state.status = (
            f"{case.case_id} -> {outcome.value} (event {self.store.last_seq} appended)"
        )
        self.state.cursor = min(self.state.cursor, max(0, len(self.queue()) - 1))

    # --------------------------------------------------------------- render

    def render(self) -> list[str]:
        """Build the whole frame. Pure: no IO, no mutation of the read model."""
        body = {
            QUEUE: self._body_queue,
            AUDIT: self._body_audit,
            TIMETRAVEL: self._body_timetravel,
            METRICS: self._body_metrics,
            RULES: self._body_rules,
            COUNTERFACTUAL: self._body_counterfactual,
            HELP: lambda: list(_HELP_LINES),
        }[self.state.view]()

        lines = [
            self.box.top(
                "SENTINEL · CQRS analyst console",
                f"log {self.store.last_seq:,} events · head {self.store.head_hash[:8]}",
            ),
            self.box.row(self._summary()),
            self.box.divider(self.state.view),
        ]
        # Frame chrome is 7 lines: top, summary, divider, divider, keybar, status, bottom.
        target = self.height - 7
        cs = self.box.charset
        for line in body[:target]:
            lines.append(self.box.row(line))
        for _ in range(target - len(body[:target])):
            lines.append(self.box.row(""))
        lines.append(self.box.divider())
        lines.append(self.box.row(self._keybar()))
        lines.append(self.box.row(self.palette.paint(f"{cs.prompt} " + self.state.status, FG_BRIGHT_GREEN)))
        lines.append(self.box.bottom())
        return lines

    def _summary(self) -> str:
        cases = self.read_model.cases
        totals = self.read_model.metrics.totals()
        outcomes = cases.outcome_counts()
        p = self.palette
        return columns(
            [
                (p.paint(f"open {cases.open_count:>4}", BOLD, FG_BRIGHT_RED), 11),
                (p.paint(f"closed {cases.closed_count:>3}", FG_GREY), 12),
                (f"txns {totals['txns']:,}", 13),
                (f"alerts {totals['alerts']:,}", 12),
                (f"cards {len(self.read_model.cards):,}", 11),
                (f"at-risk ${totals['amount_at_risk']:,.0f}", 20),
                (
                    p.paint(
                        f"fraud {outcomes.get('confirmed_fraud', 0)}"
                        f" genuine {outcomes.get('genuine', 0)}"
                        f" esc {outcomes.get('escalated', 0)}",
                        FG_MAGENTA,
                    ),
                    23,
                ),
            ]
        )

    def _keybar(self) -> str:
        return self.palette.paint(
            "[j/k] move  [f]raud [g]enuine [x] escalate  [a]udit [t]ime-travel "
            "[m]etrics [r]ules [c]ounterfactual  [q]uit",
            DIM,
        )

    # ----------------------------------------------------------- view bodies

    def _body_queue(self) -> list[str]:
        cs = self.box.charset
        cases = self.queue()
        rows = self.visible_rows()
        self.selected()
        start = max(0, min(self.state.cursor - rows // 2, max(0, len(cases) - rows)))
        window = cases[start : start + rows]
        header = self.palette.paint(
            columns(
                [
                    ("  #", 5),
                    ("CASE", 17),
                    ("CARD", 7),
                    ("AMOUNT", 10),
                    ("PRI", 8),
                    ("TRIGGER", 10),
                    ("RULES", 36),
                    ("SEQ", 8),
                ]
            ),
            BOLD,
            FG_BRIGHT_CYAN,
        )
        out = [header]
        for offset, case in enumerate(window):
            index = start + offset
            marker = cs.cursor if index == self.state.cursor else " "
            line = columns(
                [
                    (f" {marker}{index + 1:>3}", 5),
                    (case.case_id, 17),
                    (case.card_id, 7),
                    (f"{case.amount:>9,.2f}", 10),
                    (f"{case.priority:>7.4f}", 8),
                    (case.trigger, 10),
                    (",".join(case.rules) or "-", 36),
                    (f"{case.opened_seq:>7,}", 8),
                ]
            )
            if index == self.state.cursor:
                out.append(self.palette.paint(fit(line, self.box.inner), REVERSE, BOLD))
            else:
                out.append(self.palette.paint(line, self.palette.risk(case.priority)))
        if not cases:
            out.append("")
            out.append("  queue empty — every alert has been dispositioned.")
        out.append("")
        out.extend(self._detail_block())
        return out

    def _detail_block(self) -> list[str]:
        case = self.selected()
        if case is None:
            return [self.palette.paint("  no case selected", FG_GREY)]
        profile = self.read_model.cards.get(case.card_id) or {}
        head = self.palette.paint(
            f"  {case.case_id}  card {case.card_id}  ${case.amount:,.2f}  "
            f"opened t+{case.opened_at:,.0f}s  event seq {case.opened_seq:,}",
            BOLD,
        )
        scores = (
            f"    scores · ml {case.ml_score:.4f} · rules {case.rule_score:.2f} · "
            f"blended {self.policy.combine(case.ml_score, case.rule_score):.4f} "
            f"vs threshold {self.policy.alert_threshold:.3f} · trigger {case.trigger}"
        )
        card_line = (
            f"    card history · {profile.get('txns', 0):,} txns · "
            f"avg ${profile.get('avg_amount', 0):,.2f} · max ${profile.get('max_amount', 0):,.2f} · "
            f"{profile.get('alerts', 0)} alerts · {len(profile.get('categories', []))} categories"
        )
        lines = [head, scores, card_line]
        for reason in case.reasons[:2]:
            lines.append(self.palette.paint(f"    · {reason}", FG_BRIGHT_YELLOW))
        if not case.reasons:
            lines.append(self.palette.paint("    · no rule fired — model-only alert", FG_GREY))
        return lines

    def _body_audit(self) -> list[str]:
        case_id = self.state.audit_case
        if not case_id:
            return ["no case selected — press 1 to return to the queue"]
        trail = self.state.caches.setdefault("trail", {}).get(case_id)
        if trail is None:
            trail = case_trail(self.store, case_id)
            self.state.caches["trail"][case_id] = trail
        out = [
            self.palette.paint(
                f"Every recorded fact behind {case_id}, in log order. "
                "Hashes chain to the previous record.",
                BOLD,
            ),
            "",
            self.palette.paint(
                columns([("SEQ", 9), ("EVENT", 22), ("HASH", 14), ("DETAIL", 60)]),
                BOLD,
                FG_BRIGHT_CYAN,
            ),
        ]
        for entry in trail:
            out.append(
                columns(
                    [
                        (f"{entry.seq:>8,}", 9),
                        (entry.type, 22),
                        (entry.hash[:12], 14),
                        (entry.detail, 60),
                    ]
                )
            )
        out.append("")
        out.append(
            self.palette.paint(
                f"  {len(trail)} events. Nothing here was reconstructed — this is the log itself.",
                FG_GREY,
            )
        )
        return out

    def _body_timetravel(self) -> list[str]:
        case_id = self.state.travel_case
        if not case_id:
            return ["no case selected — press 1 to return to the queue"]
        cache = self.state.caches.setdefault("travel", {})
        snap = cache.get(case_id)
        if snap is None:
            snap = knowledge_at_case(self.store, case_id, self.read_model)
            cache[case_id] = snap
        out = [
            self.palette.paint(
                f"Projections rebuilt over records 1..{snap.at_seq:,} — what we knew when "
                f"{case_id} opened.",
                BOLD,
            ),
            "",
            self.palette.paint(
                columns([("FIELD", 24), ("AT THE TIME", 22), ("NOW (seq " + f"{snap.now_seq:,})", 22)]),
                BOLD,
                FG_BRIGHT_CYAN,
            ),
        ]
        for field_name in (
            "txns",
            "total_amount",
            "avg_amount",
            "max_amount",
            "alerts",
            "open_cases",
            "confirmed_fraud",
            "genuine",
            "amount_at_risk",
        ):
            then, now = snap.then.get(field_name), snap.now.get(field_name)
            changed = then != now
            row = columns([(field_name, 24), (f"{then}", 22), (f"{now}", 22)])
            out.append(self.palette.paint(row, FG_BRIGHT_YELLOW) if changed else row)
        out.append("")
        out.append(
            columns(
                [
                    ("queue depth", 24),
                    (f"{snap.queue_depth_then:,} open", 22),
                    (f"{snap.queue_depth_now:,} open", 22),
                ]
            )
        )
        out.append("")
        out.append(
            self.palette.paint(
                f"  {len(snap.deltas())} field(s) moved since. The decision must be judged on "
                "the left column.",
                FG_GREY,
            )
        )
        return out

    def _body_metrics(self) -> list[str]:
        rows = self.read_model.metrics.rows()
        totals = self.read_model.metrics.totals()
        out = [
            self.palette.paint(
                columns(
                    [
                        ("DAY", 6),
                        ("TXNS", 8),
                        ("AMOUNT", 14),
                        ("LBL FRAUD", 10),
                        ("ALERTS", 8),
                        ("RATE", 9),
                        ("AT RISK", 14),
                        ("CONFIRMED", 10),
                        ("AVOIDED", 12),
                    ]
                ),
                BOLD,
                FG_BRIGHT_CYAN,
            )
        ]
        for row in rows[: max(4, self.height - 12)]:
            out.append(
                columns(
                    [
                        (f"{row['day']:>5}", 6),
                        (f"{row['txns']:>7,}", 8),
                        (f"{row['amount']:>13,.0f}", 14),
                        (f"{row['labelled_fraud']:>9,}", 10),
                        (f"{row['alerts']:>7,}", 8),
                        (f"{row['alert_rate']:>8.4f}", 9),
                        (f"{row['amount_at_risk']:>13,.0f}", 14),
                        (f"{row['confirmed_fraud']:>9,}", 10),
                        (f"{row['loss_avoided']:>11,.0f}", 12),
                    ]
                )
            )
        out.append("")
        out.append(
            self.palette.paint(
                f"  {totals['days']} days · {totals['txns']:,} txns · {totals['alerts']:,} alerts "
                f"({totals['alert_rate']:.4%}) · ${totals['amount_at_risk']:,.0f} at risk · "
                f"${totals['loss_avoided']:,.0f} confirmed loss avoided",
                BOLD,
            )
        )
        return out

    def _body_rules(self) -> list[str]:
        out = [
            self.palette.paint(
                columns(
                    [
                        ("RULE", 30),
                        ("W", 6),
                        ("FIRES", 9),
                        ("CASES", 8),
                        ("ON FRAUD", 10),
                        ("LABEL PREC", 12),
                        ("CLOSED", 8),
                        ("ANALYST PREC", 13),
                    ]
                ),
                BOLD,
                FG_BRIGHT_CYAN,
            )
        ]
        for row in self.read_model.rules.rows():
            colour = (
                FG_BRIGHT_GREEN
                if row["label_precision"] >= 0.8
                else FG_BRIGHT_YELLOW
                if row["label_precision"] >= 0.4
                else FG_BRIGHT_RED
            )
            out.append(
                self.palette.paint(
                    columns(
                        [
                            (row["rule"], 30),
                            (f"{row['weight']:>5.2f}", 6),
                            (f"{row['fires']:>8,}", 9),
                            (f"{row['cases']:>7,}", 8),
                            (f"{row['fires_on_labelled_fraud']:>9,}", 10),
                            (f"{row['label_precision']:>11.4f}", 12),
                            (f"{row['closed']:>7,}", 8),
                            (
                                f"{row['analyst_precision']:>12.4f}"
                                if row["closed"]
                                else "           -",
                                13,
                            ),
                        ]
                    ),
                    colour,
                )
            )
        out.append("")
        out.append(
            self.palette.paint(
                "  label precision = fraction of fires on labelled fraud;  analyst precision = "
                "fraction of closed cases confirmed by a human.",
                FG_GREY,
            )
        )
        return out

    def _body_counterfactual(self) -> list[str]:
        rows = self.state.caches.get("counterfactual")
        if rows is None:
            results = replay_variants(self.store, standard_sweep(self.policy))
            rows = sweep_rows(results, fp_cost=12.0, fn_cost=450.0)
            for row, result in zip(rows, results):
                row["matches_log"] = result.matches_log
            self.state.caches["counterfactual"] = rows
        best = min(rows, key=lambda r: r["cost"])["variant"]
        out = [
            self.palette.paint(
                "Re-decided every logged score under alternative policies. Read-only: the "
                "production log is untouched.",
                BOLD,
            ),
            "",
            self.palette.paint(
                columns(
                    [
                        ("VARIANT", 34),
                        ("ALERTS", 8),
                        ("TP", 7),
                        ("FP", 7),
                        ("FN", 6),
                        ("PREC", 8),
                        ("RECALL", 8),
                        ("COST $", 12),
                    ]
                ),
                BOLD,
                FG_BRIGHT_CYAN,
            ),
        ]
        for row in rows:
            marker = "*" if row["variant"] == best else " "
            line = columns(
                [
                    (f"{marker}{row['variant']}", 34),
                    (f"{row['alerts']:>7,}", 8),
                    (f"{row['tp']:>6,}", 7),
                    (f"{row['fp']:>6,}", 7),
                    (f"{row['fn']:>5,}", 6),
                    (f"{row['precision']:>7.4f}", 8),
                    (f"{row['recall']:>7.4f}", 8),
                    (f"{row['cost']:>11,.0f}", 12),
                ]
            )
            out.append(
                self.palette.paint(line, BOLD, FG_BRIGHT_GREEN)
                if row["variant"] == best
                else line
            )
        out.append("")
        replayed = [r["variant"] for r in rows if r.get("matches_log")]
        out.append(
            self.palette.paint(
                f"  * lowest expected cost at $12/FP and $450/FN. "
                f"Reproduces the recorded alerts exactly: {', '.join(replayed) or 'none'}.",
                FG_GREY,
            )
        )
        return out
