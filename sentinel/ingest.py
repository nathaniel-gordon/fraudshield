"""Application service that turns a transaction corpus into an event log.

This is the composition root of the write side: it trains the detector, adopts a
policy, wires the bus, and feeds observations through it. It is the only module that
knows about both the detection package and the command package.

Training deliberately stays *outside* the log. A model fit is not a domain fact about
a card; it is a tool we chose. What goes on the log is the policy we adopted as a
result (``DetectionPolicyAdopted``) and every decision that policy then produced.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import pandas as pd

from .commands.bus import DEFAULT_BATCH, CommandBus
from .commands.handlers import (
    AdoptDetectionPolicyHandler,
    DispositionCaseHandler,
    ObserveTransactionHandler,
)
from .commands.messages import AdoptDetectionPolicy, DispositionCase, ObserveTransaction
from .detection.ensemble import FraudModels
from .detection.features import FEATURES, build_feature_frame
from .detection.risk_engine import RiskEngine
from .detection.synthetic import generate_transactions
from .domain.clock import SteppingClock
from .domain.policy import (
    DEFAULT_RULE_WEIGHT,
    DEFAULT_RULES_ONLY_FLOOR,
    AlertPolicy,
    CostModel,
)
from .projections.registry import ReadModel
from .store.sqlite_store import EventStore

#: Columns a real transaction export must provide. ``fraud``/``fraud_mode`` are
#: optional and only used for offline evaluation.
REQUIRED_COLUMNS = ("txn_id", "card_id", "ts", "amount", "category", "lat", "lon", "is_online")


@dataclass(frozen=True, slots=True)
class CorpusConfig:
    """Size and seed of the synthetic corpus used when no CSV is supplied."""

    n_cards: int = 600
    days: int = 45
    seed: int = 42
    fraud_rate: float = 0.012


@dataclass(slots=True)
class BuiltLog:
    """Everything a caller needs after ingestion: the log, the live read model, wiring."""

    store: EventStore
    read_model: ReadModel
    bus: CommandBus
    policy: AlertPolicy
    model_metrics: dict[str, Any]
    events_written: int
    transactions: int


def load_corpus(csv_path: str | Path | None, config: CorpusConfig) -> pd.DataFrame:
    """Read a transaction export, or synthesise one when no path is given.

    Raises:
        ValueError: when a supplied CSV is missing a required column.
    """
    if csv_path is None:
        return generate_transactions(
            n_cards=config.n_cards,
            days=config.days,
            seed=config.seed,
            fraud_rate=config.fraud_rate,
        )
    df = pd.read_csv(csv_path)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"transaction export is missing required columns: {missing}")
    if "hour" not in df.columns:
        df["hour"] = (df["ts"] / 3600.0) % 24
    if "fraud" not in df.columns:
        df["fraud"] = -1
    if "fraud_mode" not in df.columns:
        df["fraud_mode"] = ""
    return df.sort_values("ts").reset_index(drop=True)


def train_detector(
    df: pd.DataFrame, *, seed: int = 42, costs: CostModel | None = None
) -> tuple[FraudModels, dict[str, Any]]:
    """Fit the ensemble on a temporal split and cost-tune its threshold.

    Unchanged science from the pre-refactor detector: stream-safe features, first
    70% of the timeline to train, last 30% to evaluate, threshold chosen to minimise
    the cost model's ``fp_cost*FP + fn_cost*FN``.

    Raises:
        ValueError: when the corpus carries no usable fraud labels to learn from.
    """
    if "fraud" not in df.columns or int((df["fraud"] == 1).sum()) < 5:
        raise ValueError("training needs a labelled corpus with at least 5 fraud rows")
    costs = costs or CostModel()
    feat = build_feature_frame(df)
    models = FraudModels(seed=seed, fp_cost=costs.fp_cost, fn_cost=costs.fn_cost)
    metrics = models.train(feat)
    return models, metrics


def wire(
    store: EventStore,
    models: FraudModels | None,
    policy: AlertPolicy,
    read_model: ReadModel | None = None,
    *,
    clock_start: float = 0.0,
) -> tuple[CommandBus, ReadModel]:
    """Assemble the write side around an existing store.

    ``models`` may be ``None``. The analyst console only ever appends dispositions,
    which need no detector, and refusing to load one keeps that whole path free of
    scikit-learn — and of deserialising a pickle. A bus wired without a model simply
    has no handler for :class:`ObserveTransaction`, so asking it to ingest fails
    loudly rather than scoring with something stale.

    Kept separate from :func:`build_event_log` so the dashboard can attach a live bus
    to a log it merely loaded, and dispose of cases without re-ingesting.
    """
    rm = read_model if read_model is not None else ReadModel()
    bus = CommandBus(store, rm)
    bus.register(AdoptDetectionPolicy, AdoptDetectionPolicyHandler())
    if models is not None:
        bus.register(ObserveTransaction, ObserveTransactionHandler(RiskEngine(models), policy))
    bus.register(DispositionCase, DispositionCaseHandler(rm.cases, SteppingClock(clock_start)))
    return bus, rm


def build_event_log(
    df: pd.DataFrame,
    store: EventStore,
    *,
    seed: int = 42,
    costs: CostModel | None = None,
    rule_weight: float = DEFAULT_RULE_WEIGHT,
    rules_only_floor: float = DEFAULT_RULES_ONLY_FLOOR,
    batch: int = DEFAULT_BATCH,
    progress: Callable[[int, int], None] | None = None,
    models: FraudModels | None = None,
    metrics: dict[str, Any] | None = None,
) -> BuiltLog:
    """Train (if needed), adopt a policy, and stream the corpus through the bus.

    Args:
        df: time-ordered transaction corpus.
        store: destination log; it is appended to, never rewritten.
        models: a pre-trained detector to reuse, e.g. when scoring a fresh export
            against yesterday's model.
        progress: ``callable(transactions_done, events_written)``.

    Returns:
        The wiring plus the live read model, which by construction equals a replay.
    """
    costs = costs or CostModel()
    if models is None:
        models, metrics = train_detector(df, seed=seed, costs=costs)
    metrics = metrics or dict(models.metrics)

    policy = AlertPolicy(
        alert_threshold=float(models.threshold),
        rule_weight=rule_weight,
        rules_only_floor=rules_only_floor,
    )
    last_ts = float(df["ts"].max())
    bus, read_model = wire(store, models, policy, clock_start=last_ts)

    bus.submit(
        AdoptDetectionPolicy(
            model_id=f"gbm200+iforest150-seed{seed}",
            alert_threshold=policy.alert_threshold,
            rule_weight=policy.rule_weight,
            rules_only_floor=policy.rules_only_floor,
            fp_cost=costs.fp_cost,
            fn_cost=costs.fn_cost,
            auc=float(metrics.get("auc", 0.0)),
            avg_precision=float(metrics.get("avg_precision", 0.0)),
            train_rows=int(metrics.get("train_rows", 0)),
            features=tuple(FEATURES),
        )
    )
    outcome = bus.submit_many(to_commands(df), batch=batch, progress=progress)
    return BuiltLog(
        store=store,
        read_model=read_model,
        bus=bus,
        policy=policy,
        model_metrics=dict(metrics),
        events_written=outcome.records + 1,
        transactions=outcome.accepted,
    )


def to_commands(df: pd.DataFrame) -> Iterator[ObserveTransaction]:
    """Yield time-ordered observation commands.

    A generator rather than a list: the bus chunks it anyway, and materialising one
    command object per transaction alongside the frame doubles peak memory on a
    corpus of any size.
    """
    for row in df.sort_values("ts").itertuples(index=False):
        yield ObserveTransaction.from_row(row._asdict())


def alert_quality(read_model: ReadModel, df: pd.DataFrame) -> dict[str, Any]:
    """Alert-level precision/recall of the log against the corpus labels.

    Reads the case-queue projection rather than any detector internals — proof that
    the query side alone carries enough information to report detection quality.
    """
    truth = df.set_index("txn_id")["fraud"]
    alerted = {case.txn_id for case in read_model.cases.all_cases()}
    tp = sum(1 for t in alerted if int(truth.get(t, 0)) == 1)
    fp = len(alerted) - tp
    total_fraud = int((truth == 1).sum())
    fn = total_fraud - tp
    return {
        "alerts": len(alerted),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(tp / max(tp + fp, 1), 3),
        "recall": round(tp / max(total_fraud, 1), 3),
        "processed": int(read_model.metrics.totals()["txns"]),
    }
