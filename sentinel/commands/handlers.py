"""Command handlers — the only code allowed to decide that something happened.

A handler's contract is narrow on purpose: take a command, validate it, and *return*
the events it implies. It does not touch the store and it does not touch a projection.
The bus does the appending. That keeps handlers trivially unit-testable (feed a
command, inspect the events) and makes it structurally impossible for a handler to
mutate a read model behind the log's back.

Validation lives here rather than at the storage layer because a rejection must
happen *before* anything is written. Every refusal raises a subclass of
:class:`sentinel.errors.CommandRejected` carrying a stable code, and nothing is
appended when one is raised.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

from ..detection.risk_engine import RiskEngine
from ..domain.clock import Clock
from ..domain.events import (
    AlertRaised,
    CaseDispositioned,
    CaseOpened,
    DetectionPolicyAdopted,
    Disposition,
    DomainEvent,
    RiskScored,
    RuleTriggered,
    TransactionObserved,
    alert_id_for,
    case_id_for,
)
from ..domain.policy import AlertPolicy
from ..errors import CaseAlreadyClosed, OutOfOrder, UnknownCase, ValidationFailed
from ..projections.case_queue import CaseQueueProjection
from .messages import AdoptDetectionPolicy, DispositionCase, ObserveTransaction

#: Bound on a single transaction. Anything larger is a units bug or a corrupt feed,
#: not a purchase, and letting it through would drag every per-card average with it.
MAX_AMOUNT = 1_000_000.0


class Handler(Protocol):
    """Every handler maps one command to the events it justifies."""

    def handle(self, command: object) -> list[DomainEvent]:
        ...


class AdoptDetectionPolicyHandler:
    """Writes the log's self-describing header."""

    def handle(self, command: AdoptDetectionPolicy) -> list[DomainEvent]:
        _finite("rule_weight", command.rule_weight)
        _finite("alert_threshold", command.alert_threshold)
        _finite("rules_only_floor", command.rules_only_floor)
        if not 0.0 <= command.rule_weight <= 1.0:
            raise ValidationFailed(
                f"rule_weight out of range: {command.rule_weight}",
                field="rule_weight",
                value=command.rule_weight,
            )
        if not 0.0 < command.alert_threshold <= 1.0:
            raise ValidationFailed(
                "alert_threshold must be in (0, 1]",
                field="alert_threshold",
                value=command.alert_threshold,
            )
        if not 0.0 <= command.rules_only_floor <= 1.0:
            raise ValidationFailed(
                "rules_only_floor must be in [0, 1]",
                field="rules_only_floor",
                value=command.rules_only_floor,
            )
        if not command.model_id.strip():
            raise ValidationFailed("policy must name the model it adopts", field="model_id")
        if not command.features:
            raise ValidationFailed(
                "policy must list the features it was fitted on", field="features"
            )
        return [
            DetectionPolicyAdopted(
                model_id=command.model_id,
                alert_threshold=command.alert_threshold,
                rule_weight=command.rule_weight,
                rules_only_floor=command.rules_only_floor,
                fp_cost=command.fp_cost,
                fn_cost=command.fn_cost,
                auc=command.auc,
                avg_precision=command.avg_precision,
                train_rows=command.train_rows,
                features=tuple(command.features),
            )
        ]


class ObserveTransactionHandler:
    """Scores an arriving transaction and emits the facts that follow from it.

    Event order within one transaction is fixed and meaningful:
    ``TransactionObserved`` -> ``RuleTriggered``* -> ``RiskScored`` -> ``AlertRaised``
    -> ``CaseOpened``. Projections and the counterfactual replayer both rely on the
    rule fires being visible *before* the score they contributed to.
    """

    def __init__(self, risk: RiskEngine, policy: AlertPolicy) -> None:
        self.risk = risk
        self.policy = policy
        #: Newest ``occurred_at`` accepted so far. The feature stream is online and
        #: assumes arrival order equals event order; an observation from before the
        #: last one would silently corrupt every per-card window.
        self._watermark = float("-inf")

    def _validate(self, cmd: ObserveTransaction) -> None:
        """Reject anything the log must never carry.

        Finiteness first: ``json.dumps`` writes NaN and Infinity as bare literals that
        are not valid JSON, so one NaN amount would poison the record format for every
        reader that is not Python.
        """
        if not cmd.card_id.strip():
            raise ValidationFailed(f"txn {cmd.txn_id}: missing card_id", txn_id=cmd.txn_id)
        if cmd.txn_id < 0:
            raise ValidationFailed(
                f"txn_id must be non-negative, got {cmd.txn_id}", txn_id=cmd.txn_id
            )
        for name in ("amount", "occurred_at", "lat", "lon", "hour"):
            _finite(name, getattr(cmd, name), txn_id=cmd.txn_id)
        if cmd.amount < 0:
            raise ValidationFailed(
                f"txn {cmd.txn_id}: negative amount {cmd.amount}",
                txn_id=cmd.txn_id, field="amount", value=cmd.amount,
            )
        if cmd.amount > MAX_AMOUNT:
            raise ValidationFailed(
                f"txn {cmd.txn_id}: amount {cmd.amount} exceeds the {MAX_AMOUNT:,.0f} ceiling",
                txn_id=cmd.txn_id, field="amount", value=cmd.amount,
            )
        if not -90.0 <= cmd.lat <= 90.0:
            raise ValidationFailed(
                f"txn {cmd.txn_id}: latitude {cmd.lat} is off the globe",
                txn_id=cmd.txn_id, field="lat", value=cmd.lat,
            )
        if not -180.0 <= cmd.lon <= 180.0:
            raise ValidationFailed(
                f"txn {cmd.txn_id}: longitude {cmd.lon} is off the globe",
                txn_id=cmd.txn_id, field="lon", value=cmd.lon,
            )
        if not 0.0 <= cmd.hour < 24.0:
            raise ValidationFailed(
                f"txn {cmd.txn_id}: hour {cmd.hour} is not a time of day",
                txn_id=cmd.txn_id, field="hour", value=cmd.hour,
            )
        if cmd.is_online not in (0, 1):
            raise ValidationFailed(
                f"txn {cmd.txn_id}: is_online must be 0 or 1, got {cmd.is_online}",
                txn_id=cmd.txn_id, field="is_online", value=cmd.is_online,
            )
        if cmd.occurred_at < self._watermark:
            raise OutOfOrder(
                f"txn {cmd.txn_id} occurred at {cmd.occurred_at}, before the watermark "
                f"{self._watermark}",
                txn_id=cmd.txn_id, occurred_at=cmd.occurred_at, watermark=self._watermark,
            )
        self._watermark = cmd.occurred_at

    def handle(self, command: ObserveTransaction) -> list[DomainEvent]:
        return self.handle_batch([command])[0]

    def handle_batch(self, commands: Sequence[ObserveTransaction]) -> list[list[DomainEvent]]:
        """Vectorised sibling of :meth:`handle` for bulk ingestion.

        Identical semantics — per-card feature state still advances one transaction
        at a time — but one model call per batch instead of one per transaction.
        """
        for cmd in commands:
            self._validate(cmd)
        assessments = self.risk.assess_batch([c.as_txn() for c in commands])
        return [self._events_for(c, a) for c, a in zip(commands, assessments, strict=True)]

    def _events_for(self, cmd: ObserveTransaction, assessment) -> list[DomainEvent]:
        events: list[DomainEvent] = [
            TransactionObserved(
                txn_id=cmd.txn_id,
                card_id=cmd.card_id,
                occurred_at=cmd.occurred_at,
                amount=cmd.amount,
                category=cmd.category,
                lat=round(cmd.lat, 6),
                lon=round(cmd.lon, 6),
                hour=round(cmd.hour, 6),
                is_online=cmd.is_online,
                label=cmd.label,
                label_mode=cmd.label_mode,
            )
        ]
        for fire in assessment.fires:
            events.append(
                RuleTriggered(
                    txn_id=cmd.txn_id,
                    card_id=cmd.card_id,
                    rule_name=fire.name,
                    weight=fire.weight,
                    explanation=fire.explanation,
                )
            )
        combined = round(self.policy.combine(assessment.ml_score, assessment.rule_score), 6)
        events.append(
            RiskScored(
                txn_id=cmd.txn_id,
                card_id=cmd.card_id,
                ml_score=assessment.ml_score,
                rule_score=assessment.rule_score,
                combined=combined,
                alert_threshold=self.policy.alert_threshold,
                rule_weight=self.policy.rule_weight,
            )
        )
        if not self.policy.should_alert(assessment.ml_score, assessment.rule_score):
            return events

        trigger = self.policy.trigger_kind(assessment.ml_score, assessment.rule_score)
        priority = self.policy.priority(combined, cmd.amount)
        alert_id = alert_id_for(cmd.txn_id)
        events.append(
            AlertRaised(
                alert_id=alert_id,
                txn_id=cmd.txn_id,
                card_id=cmd.card_id,
                occurred_at=cmd.occurred_at,
                amount=cmd.amount,
                priority=priority,
                trigger=trigger,
            )
        )
        events.append(
            CaseOpened(
                case_id=case_id_for(cmd.txn_id),
                alert_id=alert_id,
                txn_id=cmd.txn_id,
                card_id=cmd.card_id,
                opened_at=cmd.occurred_at,
                priority=priority,
                amount=cmd.amount,
                headline=_headline(cmd, assessment, trigger),
            )
        )
        return events


def _headline(cmd: ObserveTransaction, assessment, trigger: str) -> str:
    """One-line summary the analyst sees before opening the case."""
    lead = assessment.fires[0].name if assessment.fires else trigger
    return f"{cmd.card_id} {cmd.amount:.2f} {cmd.category} :: {lead}"


class DispositionCaseHandler:
    """Closes a case, enforcing the queue's invariants.

    Reading the case-queue projection to validate is intentional: CQRS forbids the
    write side from *mutating* a read model, not from consulting one. The invariant
    "a case is dispositioned at most once" has to be checked against current state,
    and the queue projection is where current state lives.
    """

    def __init__(self, cases: CaseQueueProjection, clock: Clock) -> None:
        self.cases = cases
        self.clock = clock

    def handle(self, command: DispositionCase) -> list[DomainEvent]:
        case = self.cases.get(command.case_id)
        if case is None:
            raise UnknownCase(f"unknown case {command.case_id!r}", case_id=command.case_id)
        if not case.is_open:
            raise CaseAlreadyClosed(
                f"case {command.case_id} was already dispositioned as {case.disposition!r}",
                case_id=command.case_id, disposition=case.disposition,
            )
        if not command.analyst.strip():
            raise ValidationFailed("every disposition must name an analyst", field="analyst")
        try:
            outcome = Disposition.parse(command.disposition)
        except ValueError as exc:
            raise ValidationFailed(
                f"{command.disposition!r} is not a disposition", field="disposition"
            ) from exc
        return [
            CaseDispositioned(
                case_id=command.case_id,
                disposition=outcome.value,
                analyst=command.analyst,
                note=command.note,
                decided_at=self.clock.now(),
            )
        ]


def _finite(name: str, value: float, *, txn_id: int | None = None) -> None:
    """Reject NaN and infinity before they reach the codec."""
    if not math.isfinite(float(value)):
        raise ValidationFailed(
            f"{name} must be a finite number, got {value!r}",
            field=name, value=str(value), txn_id=txn_id,
        )


__all__ = [
    "MAX_AMOUNT",
    "AdoptDetectionPolicyHandler",
    "DispositionCaseHandler",
    "Handler",
    "ObserveTransactionHandler",
]
