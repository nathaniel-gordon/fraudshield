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

and one thing the *operations* of it needs:

    info            what this log is: size, head, schema, record mix
    cases           the queue as a table or as JSON, filtered and paged
    case            one case with the ordered facts behind it
    card            one card's profile and its case history
    stats           daily volume, alert rate and outcomes
    verify          walk the chain, and check the seal if there is one
    seal            anchor the current head with an HMAC over a key you hold
    export-jsonl    write the grep-able archive
    import-jsonl    rebuild a log from an archive, re-checking every link

Every command is offline and deterministic for a given seed. Anything that produces a
list accepts ``--json``, so the operational commands compose with ``jq`` rather than
only with a human.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from . import paths
from .domain.events import DetectionPolicyAdopted, Disposition
from .domain.policy import DEFAULT_FN_COST, DEFAULT_FP_COST, AlertPolicy, CostModel
from .errors import SentinelError
from .ingest import BuiltLog, CorpusConfig, alert_quality, build_event_log, load_corpus, wire
from .obs import configure_logging, correlation_scope, get_logger
from .projections.registry import ReadModel
from .query.audit import case_trail
from .query.counterfactual import replay_variants, standard_sweep, sweep_rows
from .query.operations import card_report, case_detail, list_cases, log_summary, stats
from .query.timetravel import knowledge_at_case
from .store.jsonl import export_jsonl, import_jsonl
from .store.seal import ENV_KEY, load_key, seal, seals, verify_seals
from .store.sqlite_store import EventStore
from .ui.ansi import Palette, configure_stdout, supports_color
from .ui.dashboard import Dashboard
from .ui.runner import DEMO_SCRIPT, run_live, run_scripted

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"


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


def _costs(args: argparse.Namespace) -> CostModel:
    return CostModel(fp_cost=args.fp_cost, fn_cost=args.fn_cost)


def _rule(title: str) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")


def _emit(payload: dict[str, Any] | list[Any]) -> None:
    """Machine-readable output on stdout. One document, so ``jq`` can read it."""
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


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
    ("f1", 7, ".4f"),
    ("cost", 12, ",.0f"),
]

CASE_COLUMNS: list[tuple[str, int, str]] = [
    ("case_id", 15, ""),
    ("card_id", 8, ""),
    ("priority", 9, ".4f"),
    ("amount", 10, ",.2f"),
    ("trigger", 11, ""),
    ("disposition", 16, ""),
    ("headline", 42, ""),
]

DAY_COLUMNS: list[tuple[str, int, str]] = [
    ("day", 5, ",d"),
    ("txns", 8, ",d"),
    ("amount", 13, ",.0f"),
    ("alerts", 7, ",d"),
    ("alert_rate", 11, ".4f"),
    ("amount_at_risk", 15, ",.0f"),
    ("cases_opened", 13, ",d"),
    ("confirmed_fraud", 16, ",d"),
    ("loss_avoided", 13, ",.0f"),
]


def _table(rows: list[dict[str, Any]], spec: list[tuple[str, int, str]]) -> None:
    print("  " + "  ".join(key.upper().ljust(width) for key, width, _ in spec))
    for row in rows:
        cells = []
        for key, width, fmt in spec:
            value = row.get(key)
            text = format(value, fmt) if fmt and value is not None else str(value or "")
            cells.append(text.ljust(width)[:width])
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

    costs = _costs(args)
    built = build_event_log(df, store, seed=args.seed, costs=costs, progress=progress)
    # The fitted model is deliberately *not* written to disk. Nothing reads it back —
    # the log carries every score that was ever computed — and a joblib file next to
    # the log is a pickle that some later command would be tempted to load.
    if not quiet:
        print()
        print(f"  model: {built.model_metrics}")
        print(f"  log:   {built.events_written:,} events -> {store.path}")
    return Ingested(built=built, corpus=df)


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
    was ever computed, so the whole query side runs without scikit-learn — and, more
    to the point, without deserialising a pickle.
    """
    path = paths.input_file(log_path)
    store = EventStore(path)
    header = store.header()
    if header is None or not isinstance(header.event, DetectionPolicyAdopted):
        store.close()
        raise SystemExit("log has no DetectionPolicyAdopted header; it was not built by this tool")
    policy = AlertPolicy(
        alert_threshold=header.event.alert_threshold,
        rule_weight=header.event.rule_weight,
        rules_only_floor=header.event.rules_only_floor,
    )
    return store, ReadModel.rebuild(store), policy


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Open the console. Dispositions are the only thing it writes, and they need no model."""
    store, read_model, policy = _open_existing(args.log)
    last_ts = max((c.opened_at for c in read_model.cases.all_cases()), default=0.0)
    bus, _ = wire(store, None, policy, read_model, clock_start=last_ts)
    dashboard = Dashboard(store, read_model, bus, policy, costs=_costs(args),
                          width=args.width, height=args.height, analyst=args.analyst)
    try:
        recorded = run_live(dashboard)
    finally:
        store.close()
    print(f"session over — {recorded} disposition(s) appended to {args.log}")
    return 0


# ---------------------------------------------------------------- inspection


def cmd_replay(args: argparse.Namespace) -> int:
    store = EventStore(paths.input_file(args.log))
    try:
        verified = store.verify()
        first, second = ReadModel.rebuild(store), ReadModel.rebuild(store)
        if args.json:
            _emit({
                "records_verified": verified,
                "head_hash": store.head_hash,
                "stable_across_rebuilds": first.fingerprint() == second.fingerprint(),
                "fingerprints": first.fingerprints(),
            })
            return 0
        _rule("REPLAY — rebuild every projection from the log alone")
        print(f"  hash chain verified over {verified:,} records; head {store.head_hash[:16]}")
        print(f"  rebuilt twice, fingerprints identical: "
              f"{first.fingerprint() == second.fingerprint()}")
        for name, digest in first.fingerprints().items():
            print(f"    {name:<18} {digest}")
    finally:
        store.close()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    store = EventStore(paths.input_file(args.log))
    try:
        trail = case_trail(store, args.case)
        if not trail:
            raise SystemExit(f"case {args.case} is not present in {args.log}")
        if args.json:
            _emit([entry.as_dict() for entry in trail])
            return 0
        _rule(f"AUDIT TRAIL — {args.case}")
        for entry in trail:
            print(f"  {entry.seq:>8,}  {entry.type:<22} {entry.hash[:12]}  {entry.detail}")
    finally:
        store.close()
    return 0


def cmd_counterfactual(args: argparse.Namespace) -> int:
    store, _, policy = _open_existing(args.log)
    try:
        results = replay_variants(store, standard_sweep(policy))
        rows = sweep_rows(results, _costs(args))
        exact = [r.label for r in results if r.matches_log]
        if args.json:
            _emit({"costs": _costs(args).as_dict(), "variants": rows, "reproduce_log": exact})
            return 0
        _rule("COUNTERFACTUAL — re-decide the whole log under alternative policies")
        _table(rows, SWEEP_COLUMNS)
        print(f"\n  reproduces the recorded alert set exactly: {', '.join(exact) or 'none'}")
    finally:
        store.close()
    return 0


# ------------------------------------------------------------------ analyst


def cmd_info(args: argparse.Namespace) -> int:
    store = EventStore(paths.input_file(args.log))
    try:
        summary = log_summary(store)
        summary["seals"] = [entry.as_dict() for entry in seals(store)]
        if args.json:
            _emit(summary)
            return 0
        _rule(f"LOG — {summary['path']}")
        print(f"  records          {summary['records']:,}")
        print(f"  head hash        {summary['head_hash']}")
        print(f"  schema version   {summary['schema_version']}")
        print(f"  commands applied {summary['commands_applied']:,}")
        print(f"  seals            {len(summary['seals'])}")
        print("  record mix:")
        for type_name, count in sorted(summary["type_counts"].items()):
            print(f"    {type_name:<24} {count:>10,}")
    finally:
        store.close()
    return 0


def cmd_cases(args: argparse.Namespace) -> int:
    store, read_model, _ = _open_existing(args.log)
    try:
        page = list_cases(
            read_model,
            status=args.status,
            card_id=args.card or "",
            min_priority=args.min_priority,
            offset=args.offset,
            limit=args.limit,
        )
        if args.json:
            _emit(page.as_dict())
            return 0
        _rule(f"CASES — {args.status}, showing {len(page.items)} of {page.total:,}")
        _table(page.items, CASE_COLUMNS)
        if page.next_offset is not None:
            print(f"\n  more: --offset {page.next_offset}")
    finally:
        store.close()
    return 0


def cmd_case(args: argparse.Namespace) -> int:
    store, read_model, _ = _open_existing(args.log)
    try:
        detail = case_detail(store, read_model, args.case)
        if args.json:
            _emit(detail)
            return 0
        case = detail["case"]
        _rule(f"CASE — {case['case_id']}")
        for key in ("card_id", "txn_id", "priority", "amount", "trigger", "disposition",
                    "analyst", "headline"):
            print(f"  {key:<14} {case[key]}")
        for reason in case["reasons"] or ["(model-only alert — no rule fired)"]:
            print(f"    reason: {reason}")
        print("\n  trail:")
        for entry in detail["trail"]:
            print(f"    {entry['seq']:>8,}  {entry['type']:<22} {entry['hash']}  {entry['detail']}")
    finally:
        store.close()
    return 0


def cmd_card(args: argparse.Namespace) -> int:
    store, read_model, _ = _open_existing(args.log)
    try:
        report = card_report(store, read_model, args.card, offset=args.offset, limit=args.limit)
        if args.json:
            _emit(report)
            return 0
        _rule(f"CARD — {report['card_id']}")
        for key, value in sorted(report["profile"].items()):
            print(f"  {key:<18} {value}")
        print(f"  stream version     {report['stream_version']}")
        cases = report["cases"]
        print(f"\n  cases ({len(cases['items'])} of {cases['total']:,}):")
        _table(cases["items"], CASE_COLUMNS)
        if cases["next_offset"] is not None:
            print(f"\n  more: --offset {cases['next_offset']}")
    finally:
        store.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    store, read_model, _ = _open_existing(args.log)
    try:
        report = stats(read_model, since_day=args.since_day)
        if args.json:
            _emit(report)
            return 0
        _rule("STATS — folded from the log")
        for key, value in report["totals"].items():
            print(f"  {key:<18} {value}")
        print(f"\n  queue: {report['queue']['open']:,} open, {report['queue']['closed']:,} closed, "
              f"outcomes {report['queue']['outcomes']}")
        print()
        _table(report["days"], DAY_COLUMNS)
    finally:
        store.close()
    return 0


# ------------------------------------------------------------------ evidence


def cmd_verify(args: argparse.Namespace) -> int:
    store = EventStore(paths.input_file(args.log))
    try:
        verified = store.verify()
        result: dict[str, Any] = {
            "records_verified": verified,
            "head_hash": store.head_hash,
            "chain": "ok",
        }
        if args.key or args.require_seal or seals(store):
            check = verify_seals(store, load_key(args.key), require=args.require_seal)
            result["seal"] = check.as_dict()
        if args.json:
            _emit(result)
            return 0
        _rule(f"VERIFY — {store.path}")
        print(f"  chain: {verified:,} records verified, head {store.head_hash}")
        if "seal" in result:
            print(f"  seal:  {result['seal']['detail']}")
        else:
            print(f"  seal:  none — run `seal` with {ENV_KEY} set to anchor this head")
    finally:
        store.close()
    return 0


def cmd_seal(args: argparse.Namespace) -> int:
    store = EventStore(paths.input_file(args.log))
    try:
        entry = seal(store, load_key(args.key))
        if args.json:
            _emit(entry.as_dict())
            return 0
        _rule(f"SEAL — {store.path}")
        print(f"  sealed records 1..{entry.seq:,} ({entry.record_count:,} records)")
        print(f"  head {entry.head_hash}")
        print(f"  mac  {entry.mac}  (key {entry.key_id})")
        print("\n  Keep this receipt somewhere the log's writer cannot reach it.")
    finally:
        store.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = EventStore(paths.input_file(args.log))
    try:
        target = paths.output_file(args.to)
        written = export_jsonl(store, target)
        payload = {"exported": written, "path": str(target), "head_hash": store.head_hash}
        if args.json:
            _emit(payload)
        else:
            print(f"  exported {written:,} records -> {target}")
    finally:
        store.close()
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    source = paths.input_file(getattr(args, "from"))
    store = EventStore.create_fresh(paths.output_file(args.log))
    try:
        report = import_jsonl(source, store, recover=args.recover)
        payload = report.as_dict() | {"path": str(store.path)}
        if args.json:
            _emit(payload)
        else:
            print(f"  imported {report.imported:,} records -> {store.path}")
            print(f"  head {report.head_hash}")
            if report.skipped_tail_bytes:
                print(f"  dropped {report.skipped_tail_bytes:,} bytes of torn tail")
    finally:
        store.close()
    return 0


# ----------------------------------------------------------------------- demo


def cmd_demo(args: argparse.Namespace) -> int:
    """End-to-end tour that terminates on its own."""
    costs = _costs(args)
    _rule("1/6  BUILD — command side: every decision becomes an immutable event")
    session = ingest(args)
    store, read_model, policy = session.store, session.read_model, session.policy
    quality = alert_quality(read_model, session.corpus)
    print(f"  detection quality (alert level): precision {quality['precision']} "
          f"recall {quality['recall']} — {quality['tp']} TP / {quality['fp']} FP / {quality['fn']} FN")

    _rule("2/6  TUI — scripted analyst session (the same dashboard the live mode drives)")
    dashboard = Dashboard(store, read_model, session.built.bus, policy, costs=costs,
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
        print(f"  {field_name:<18} {then!s:>16} {now!s:>16}")
    print(f"  queue depth then {snap.queue_depth_then:,} vs now {snap.queue_depth_now:,}")

    _rule("5/6  AUDIT TRAIL — the exact ordered facts behind that case")
    for entry in case_trail(store, target):
        print(f"  {entry.seq:>8,}  {entry.type:<22} {entry.hash[:12]}  {entry.detail}")

    _rule("6/6  COUNTERFACTUAL — alternative policies scored against the same log")
    results = replay_variants(store, standard_sweep(policy))
    rows = sweep_rows(results, costs)
    _table(rows, SWEEP_COLUMNS)
    exact = [r.label for r in results if r.matches_log]
    print(f"\n  reproduces the recorded alert set exactly: {', '.join(exact) or 'none'}")
    cheapest = min(rows, key=lambda r: r["cost"])
    print(f"  cheapest at ${costs.fp_cost:.0f}/FP and ${costs.fn_cost:.0f}/FN: "
          f"{cheapest['variant']} (${cheapest['cost']:,.0f} vs ${rows[0]['cost']:,.0f} "
          f"for the recorded policy)")

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
        (f"Event log `{store.path.name}`: {store.last_seq:,} records, "
         f"chain head `{store.head_hash[:16]}`."),
        "",
        "## Detection quality",
        "",
        (f"- model on the temporal holdout: AUC **{metrics['auc']}**, average precision "
         f"**{metrics['avg_precision']}**, cost-tuned threshold **{metrics['threshold']}**"),
        (f"- alert level over the whole stream: precision **{quality['precision']}**, recall "
         f"**{quality['recall']}** ({quality['tp']} TP / {quality['fp']} FP / "
         f"{quality['fn']} FN)"),
        (f"- policy in force: threshold {policy.alert_threshold:.3f}, rule weight "
         f"{policy.rule_weight:.2f}, rules-only floor {policy.rules_only_floor:.2f}"),
        "",
        "The corpus is synthetic. These numbers describe the generator as much as the",
        "detector; see the Limitations section of the README.",
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
              "| variant | alerts | tp | fp | fn | precision | recall | f1 | cost $ |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in sweep:
        lines.append(
            f"| {row['variant']} | {row['alerts']:,} | {row['tp']:,} | {row['fp']:,} | "
            f"{row['fn']:,} | {row['precision']:.4f} | {row['recall']:.4f} | {row['f1']:.4f} | "
            f"{row['cost']:,.0f} |"
        )
    lines += ["", f"## Audit trail — {case_id}", "", "```"]
    lines += [f"{e.seq:>8}  {e.type:<22} {e.hash[:12]}  {e.detail}"
              for e in case_trail(store, case_id)]
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
    p.add_argument("--json", action="store_true", help="emit machine-readable output")
    p.add_argument("--log-level", default=None,
                   help="structured-log level on stderr (DEBUG/INFO/WARNING/ERROR)")
    p.add_argument("--fp-cost", type=float, default=DEFAULT_FP_COST,
                   help="dollars of analyst time per false alert")
    p.add_argument("--fn-cost", type=float, default=DEFAULT_FN_COST,
                   help="dollars lost per missed fraud")
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
    sub.add_parser("info", help="what this log is: size, head, schema, record mix")

    c = sub.add_parser("cases", help="the case queue, filtered and paged")
    c.add_argument("--status", default="open", choices=("open", "closed", "all"))
    c.add_argument("--card", default=None, help="restrict to one card")
    c.add_argument("--min-priority", type=float, default=0.0)
    _paging(c)

    one = sub.add_parser("case", help="one case with the facts behind it")
    one.add_argument("--case", required=True)

    card = sub.add_parser("card", help="one card's profile and case history")
    card.add_argument("--card", required=True)
    _paging(card)

    st = sub.add_parser("stats", help="daily volume, alert rate and outcomes")
    st.add_argument("--since-day", type=int, default=None)

    v = sub.add_parser("verify", help="walk the chain and check any seal")
    v.add_argument("--key", default=None, help=f"sealing key; defaults to ${ENV_KEY}")
    v.add_argument("--require-seal", action="store_true",
                   help="fail when the log carries no seal at all")

    sl = sub.add_parser("seal", help="anchor the current head with an HMAC")
    sl.add_argument("--key", default=None, help=f"sealing key; defaults to ${ENV_KEY}")

    ex = sub.add_parser("export-jsonl", help="write the grep-able archive")
    ex.add_argument("--to", required=True)

    im = sub.add_parser("import-jsonl", help="rebuild a log from an archive")
    im.add_argument("--from", required=True, dest="from")
    im.add_argument("--recover", action="store_true",
                    help="drop an incomplete final line instead of failing")
    return p


def _paging(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "build": cmd_build,
    "demo": cmd_demo,
    "dashboard": cmd_dashboard,
    "replay": cmd_replay,
    "audit": cmd_audit,
    "counterfactual": cmd_counterfactual,
    "info": cmd_info,
    "cases": cmd_cases,
    "case": cmd_case,
    "card": cmd_card,
    "stats": cmd_stats,
    "verify": cmd_verify,
    "seal": cmd_seal,
    "export-jsonl": cmd_export,
    "import-jsonl": cmd_import,
}


def main(argv: list[str] | None = None) -> int:
    # Must happen before the first frame: a legacy Windows code page cannot encode
    # box-drawing glyphs, and the console must never be able to kill the process.
    configure_stdout()
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    with correlation_scope() as cid:
        log.info("command_started", extra={"command": args.cmd})
        try:
            return COMMANDS[args.cmd](args)
        except SentinelError as exc:
            log.error("command_failed", extra={"command": args.cmd, **exc.log_fields()})
            if args.json:
                print(json.dumps(exc.as_dict() | {"correlation_id": cid}, sort_keys=True))
            else:
                print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
