"""Launcher for the analyst console and the log-maintenance subcommands.

The *interface* of this project is the TUI; argparse here is only the door. Each
subcommand maps to one thing the architecture makes possible:

    build           ingest a corpus into the append-only log
    dashboard       open the live console on an existing log
    demo            build, run a scripted console tour, then prove replay, time travel
                    and counterfactual policy scoring — and exit on its own
    replay          rebuild every projection from the log and compare fingerprints
    audit           print one case's ordered event trail
    counterfactual  re-decide the whole log under alternative policies

Every command is offline and deterministic for a given seed.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .detection.ensemble import FraudModels
from .domain.events import Disposition
from .errors import InvalidPath
from .domain.policy import AlertPolicy
from .ingest import BuiltLog, CorpusConfig, alert_quality, build_event_log, load_corpus, wire
from .projections.registry import ReadModel
from .query.audit import case_trail
from .query.counterfactual import replay_variants, standard_sweep, sweep_rows
from .query.timetravel import knowledge_at_case
from . import paths
from .store.sqlite_store import EventStore
from .ui.ansi import Palette, configure_stdout, supports_color
from .ui.dashboard import Dashboard
from .ui.runner import DEMO_SCRIPT, run_live, run_scripted

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
DETECTOR_PATH = OUT / "detector.joblib"

#: Cost model used throughout the reports, matching the detector's threshold tuning.
FP_COST, FN_COST = 12.0, 450.0


@dataclass(slots=True)
class Ingested:
    """Result of a build: the log, its live read model, and the corpus behind it."""

    built: BuiltLog
    corpus: pd.DataFrame

    @property
    def store(self) -> EventStore:
        return self.built.store

    @property
    def read_model(self) -> ReadModel:
        return self.built.read_model

    @property
    def policy(self) -> AlertPolicy:
        return self.built.policy


def _rule(title: str) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")


#: (key, width, format spec) for the counterfactual sweep table. Rates want four
#: decimals; money wants none — one shared float format would make both unreadable.
SWEEP_COLUMNS: list[tuple[str, int, str]] = [
    ("variant", 34, ""),
    ("alerts", 7, ",d"),
    ("tp", 6, ",d"),
    ("fp", 6, ",d"),
    ("fn", 5, ",d"),
    ("precision", 9, ".4f"),
    ("recall", 8, ".4f"),
    ("cost", 12, ",.0f"),
]


def _table(rows: list[dict[str, Any]], spec: list[tuple[str, int, str]]) -> None:
    print("  " + "  ".join(key.upper().ljust(width) for key, width, _ in spec))
    for row in rows:
        cells = [
            format(row[key], fmt).ljust(width)[:width] if fmt else str(row[key]).ljust(width)[:width]
            for key, width, fmt in spec
        ]
        print("  " + "  ".join(cells))


# --------------------------------------------------------------------- build


def ingest(args: argparse.Namespace, *, quiet: bool = False) -> Ingested:
    """Train, adopt a policy, and stream a corpus into a fresh log."""
    OUT.mkdir(exist_ok=True)
    config = CorpusConfig(n_cards=args.cards, days=args.days, seed=args.seed)
    df = load_corpus(args.data, config)
    if not quiet:
        modes = df[df["fraud"] == 1]["fraud_mode"].value_counts().to_dict()
        print(f"  corpus: {len(df):,} transactions, {int((df['fraud'] == 1).sum())} labelled fraud "
              f"({(df['fraud'] == 1).mean():.2%}) across {modes}")
    store = EventStore.create_fresh(paths.output_file(args.log))

    def progress(done: int, events: int) -> None:
        if not quiet:
            print(f"\r  ingesting … {done:,}/{len(df):,} transactions -> {events:,} events",
                  end="", flush=True)

    built = build_event_log(df, store, seed=args.seed, fp_cost=FP_COST, fn_cost=FN_COST,
                            progress=progress)
    _save_detector(built)
    if not quiet:
        print()
        print(f"  model: {built.model_metrics}")
        print(f"  log:   {built.events_written:,} events -> {store.path}")
    return Ingested(built=built, corpus=df)


def _save_detector(built: BuiltLog) -> None:
    """Persist the fitted ensemble next to the log."""
    from .commands.messages import ObserveTransaction

    handler = built.bus.handler_for(ObserveTransaction)
    handler.risk.models.save(DETECTOR_PATH)  # type: ignore[attr-defined]


def cmd_build(args: argparse.Namespace) -> int:
    _rule("BUILD — command side: every decision becomes an immutable event")
    session = ingest(args)
    print(f"  alerts: {alert_quality(session.read_model, session.corpus)}")
    session.store.close()
    return 0


# ----------------------------------------------------------------- dashboard


def _open_existing(log_path: str) -> tuple[EventStore, ReadModel, AlertPolicy]:
    """Attach to a log written by an earlier run, rebuilding the read model by replay.

    Note what is *not* loaded here: the model file. The log carries every score that
    was ever computed, so the whole query side runs without scikit-learn.
    """
    try:
        path = paths.input_file(log_path)
    except InvalidPath as exc:
        raise SystemExit(f"{exc.message}: {log_path} — run `python -m sentinel build` first") from exc
    store = EventStore(path)
    header = next(store.read(upto_seq=1), None)
    if header is None or header.type != "DetectionPolicyAdopted":
        store.close()
        raise SystemExit("log has no DetectionPolicyAdopted header; it was not built by this tool")
    policy = AlertPolicy(
        alert_threshold=header.event.alert_threshold,
        rule_weight=header.event.rule_weight,
        rules_only_floor=header.event.rules_only_floor,
    )
    return store, ReadModel.rebuild(store), policy


def cmd_dashboard(args: argparse.Namespace) -> int:
    store, read_model, policy = _open_existing(args.log)
    if not DETECTOR_PATH.exists():
        store.close()
        raise SystemExit(f"dashboard needs {DETECTOR_PATH} — run `python -m sentinel build` first")
    last_ts = max((c.opened_at for c in read_model.cases.all_cases()), default=0.0)
    bus, _ = wire(store, FraudModels.load(DETECTOR_PATH), policy, read_model, clock_start=last_ts)
    dashboard = Dashboard(store, read_model, bus, policy,
                          width=args.width, height=args.height, analyst=args.analyst)
    try:
        recorded = run_live(dashboard)
    finally:
        store.close()
    print(f"session over — {recorded} disposition(s) appended to {args.log}")
    return 0


# ---------------------------------------------------------------- inspection


def cmd_replay(args: argparse.Namespace) -> int:
    _rule("REPLAY — rebuild every projection from the log alone")
    store = EventStore(paths.input_file(args.log))
    verified = store.verify()
    first, second = ReadModel.rebuild(store), ReadModel.rebuild(store)
    print(f"  hash chain verified over {verified:,} records; head {store.head_hash[:16]}")
    print(f"  rebuilt twice, fingerprints identical: {first.fingerprint() == second.fingerprint()}")
    for name, digest in first.fingerprints().items():
        print(f"    {name:<18} {digest}")
    store.close()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    _rule(f"AUDIT TRAIL — {args.case}")
    store = EventStore(paths.input_file(args.log))
    trail = case_trail(store, args.case)
    store.close()
    if not trail:
        raise SystemExit(f"case {args.case} is not present in {args.log}")
    for entry in trail:
        print(f"  {entry.seq:>8,}  {entry.type:<22} {entry.hash[:12]}  {entry.detail}")
    return 0


def cmd_counterfactual(args: argparse.Namespace) -> int:
    _rule("COUNTERFACTUAL — re-decide the whole log under alternative policies")
    store, _, policy = _open_existing(args.log)
    results = replay_variants(store, standard_sweep(policy))
    rows = sweep_rows(results, fp_cost=FP_COST, fn_cost=FN_COST)
    _table(rows, SWEEP_COLUMNS)
    exact = [r.label for r in results if r.matches_log]
    print(f"\n  reproduces the recorded alert set exactly: {', '.join(exact) or 'none'}")
    store.close()
    return 0


# ----------------------------------------------------------------------- demo


def cmd_demo(args: argparse.Namespace) -> int:
    """End-to-end tour that terminates on its own."""
    _rule("1/6  BUILD — command side: every decision becomes an immutable event")
    session = ingest(args)
    store, read_model, policy = session.store, session.read_model, session.policy
    quality = alert_quality(read_model, session.corpus)
    print(f"  detection quality (alert level): precision {quality['precision']} "
          f"recall {quality['recall']} — {quality['tp']} TP / {quality['fp']} FP / {quality['fn']} FN")

    _rule("2/6  TUI — scripted analyst session (the same dashboard the live mode drives)")
    dashboard = Dashboard(store, read_model, session.built.bus, policy,
                          width=args.width, height=args.height,
                          palette=Palette(supports_color(sys.stdout)), analyst="a.okafor")
    frames = run_scripted(dashboard, DEMO_SCRIPT, out=sys.stdout, show_frames=args.frames)
    print(f"\n  {len(frames)} frames rendered · {dashboard.state.dispositions} cases dispositioned "
          f"by keystroke · the session ended by itself.")

    _rule("3/6  REPLAY — the live read model vs one rebuilt from nothing but the log")
    verified = store.verify()
    rebuilt = ReadModel.rebuild(store)
    live_fp, replay_fp = read_model.fingerprint(), rebuilt.fingerprint()
    print(f"  hash chain verified over {verified:,} records")
    print(f"  live     fingerprint {live_fp}")
    print(f"  replayed fingerprint {replay_fp}")
    print(f"  byte-identical: {live_fp == replay_fp}")
    if live_fp != replay_fp:
        differing = [n for n, d in rebuilt.fingerprints().items()
                     if d != read_model.fingerprints()[n]]
        raise SystemExit(f"REPLAY DIVERGED on {differing} — a projection has hidden state")

    _rule("4/6  TIME TRAVEL — what did we know when this case opened?")
    target = next((c.case_id for c in read_model.cases.all_cases()
                   if c.disposition == Disposition.CONFIRMED_FRAUD.value), None)
    target = target or read_model.cases.open_cases(1)[0].case_id
    snap = knowledge_at_case(store, target, read_model)
    print(f"  {target} on card {snap.card_id}: projections rebuilt over records "
          f"1..{snap.at_seq:,} of {snap.now_seq:,}")
    print(f"  {'field':<18} {'at the time':>16} {'now':>16}")
    for field_name, (then, now) in snap.deltas().items():
        print(f"  {field_name:<18} {str(then):>16} {str(now):>16}")
    print(f"  queue depth then {snap.queue_depth_then:,} vs now {snap.queue_depth_now:,}")

    _rule("5/6  AUDIT TRAIL — the exact ordered facts behind that case")
    for entry in case_trail(store, target):
        print(f"  {entry.seq:>8,}  {entry.type:<22} {entry.hash[:12]}  {entry.detail}")

    _rule("6/6  COUNTERFACTUAL — alternative policies scored against the same log")
    results = replay_variants(store, standard_sweep(policy))
    rows = sweep_rows(results, fp_cost=FP_COST, fn_cost=FN_COST)
    _table(rows, SWEEP_COLUMNS)
    exact = [r.label for r in results if r.matches_log]
    print(f"\n  reproduces the recorded alert set exactly: {', '.join(exact) or 'none'}")
    cheapest = min(rows, key=lambda r: r["cost"])
    print(f"  cheapest at ${FP_COST:.0f}/FP and ${FN_COST:.0f}/FN: {cheapest['variant']} "
          f"(${cheapest['cost']:,.0f} vs ${rows[0]['cost']:,.0f} for the recorded policy)")

    _write_artifacts(store, read_model, policy, session.built.model_metrics, quality, rows, target)
    store.close()
    print(f"\n  artifacts -> {OUT}")
    return 0


def _write_artifacts(store: EventStore, read_model: ReadModel, policy: AlertPolicy,
                     metrics: dict, quality: dict, sweep: list[dict], case_id: str) -> None:
    """Persist the log-derived reports. The log itself is already on disk."""
    (OUT / "projections.json").write_text(
        json.dumps(read_model.snapshot(), indent=1, sort_keys=True), encoding="utf-8"
    )
    lines = [
        "# Fraud Sentinel — event-sourced run report",
        "",
        f"Event log `{store.path.name}`: {store.last_seq:,} records, "
        f"chain head `{store.head_hash[:16]}`.",
        "",
        "## Detection quality",
        "",
        f"- model on the temporal holdout: AUC **{metrics['auc']}**, average precision "
        f"**{metrics['avg_precision']}**, cost-tuned threshold **{metrics['threshold']}**",
        f"- alert level over the whole stream: precision **{quality['precision']}**, recall "
        f"**{quality['recall']}** ({quality['tp']} TP / {quality['fp']} FP / {quality['fn']} FN)",
        f"- policy in force: threshold {policy.alert_threshold:.3f}, rule weight "
        f"{policy.rule_weight:.2f}, rules-only floor {policy.rules_only_floor:.2f}",
        "",
        "## Read-model fingerprints (rebuilt purely by replay)",
        "",
    ]
    lines += [f"- `{name}` — `{digest}`" for name, digest in read_model.fingerprints().items()]
    lines += ["", "## Rule performance", "",
              "| rule | fires | on labelled fraud | label precision | closed | analyst precision |",
              "|---|---:|---:|---:|---:|---:|"]
    for row in read_model.rules.rows():
        lines.append(
            f"| {row['rule']} | {row['fires']:,} | {row['fires_on_labelled_fraud']:,} | "
            f"{row['label_precision']:.4f} | {row['closed']} | {row['analyst_precision']:.4f} |"
        )
    lines += ["", "## Counterfactual policy replay", "",
              "| variant | alerts | tp | fp | fn | precision | recall | cost $ |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in sweep:
        lines.append(
            f"| {row['variant']} | {row['alerts']:,} | {row['tp']:,} | {row['fp']:,} | "
            f"{row['fn']:,} | {row['precision']:.4f} | {row['recall']:.4f} | {row['cost']:,.0f} |"
        )
    lines += ["", f"## Audit trail — {case_id}", "", "```"]
    lines += [f"{e.seq:>8}  {e.type:<22} {e.hash[:12]}  {e.detail}" for e in case_trail(store, case_id)]
    lines += ["```", ""]
    (OUT / "run_report.md").write_text("\n".join(lines), encoding="utf-8")


# ------------------------------------------------------------------- parsing


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m sentinel", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", default=str(OUT / "events.db"), help="path to the event log")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--width", type=int, default=112, help="console width in columns")
    p.add_argument("--height", type=int, default=30, help="console height in rows")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, help_text in (("build", "ingest a corpus into a fresh event log"),
                            ("demo", "scripted end-to-end tour that terminates on its own")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("--data", default=None,
                       help="transaction CSV; omit for the built-in synthetic corpus")
        s.add_argument("--cards", type=int, default=600)
        s.add_argument("--days", type=int, default=45)
        if name == "demo":
            s.add_argument("--frames", type=int, nargs="*", default=None,
                           help="which scripted frames to print (default: all)")

    d = sub.add_parser("dashboard", help="open the live analyst console on an existing log")
    d.add_argument("--analyst", default="analyst", help="name recorded on dispositions")

    sub.add_parser("replay", help="rebuild projections from the log and print fingerprints")
    a = sub.add_parser("audit", help="print one case's ordered event trail")
    a.add_argument("--case", required=True)
    sub.add_parser("counterfactual", help="re-decide the log under alternative policies")
    return p


def main(argv: list[str] | None = None) -> int:
    # Must happen before the first frame: a legacy Windows code page cannot encode
    # box-drawing glyphs, and the console must never be able to kill the process.
    configure_stdout()
    args = build_parser().parse_args(argv)
    return {
        "build": cmd_build,
        "demo": cmd_demo,
        "dashboard": cmd_dashboard,
        "replay": cmd_replay,
        "audit": cmd_audit,
        "counterfactual": cmd_counterfactual,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
