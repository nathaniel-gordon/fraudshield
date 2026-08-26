"""Command handlers — the only code allowed to decide that something happened.

A handler's contract is narrow on purpose: take a command, validate it, and *return*
the events it implies. It does not touch the store and it does not touch a projection.
The bus does the appending. That keeps handlers trivially unit-testable (feed a
command, inspect the events) and makes it structurally impossible for a handler to
mutate a read model behind the log's back.
"""
from __future__ import annotations

from typing import Protocol, Sequence

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
from ..projections.case_queue import CaseQueueProjection
from .messages import AdoptDetectionPolicy, DispositionCase, ObserveTransaction


class CommandRejected(Exception):
    """A command failed validation; nothing was appended to the log.

    Rejections are not events. A refused instruction is a non-event, and writing
    it would pollute the fact stream that projections fold.
    """


class Handler(Protocol):
    """Every handler maps one command to the events it justifies."""

    def handle(self, command: object) -> list[DomainEvent]:
        ...


class AdoptDetectionPolicyHandler:
    """Writes the log's self-describing header."""

    def handle(self, command: AdoptDetectionPolicy) -> list[DomainEvent]:
        if not 0.0 <= command.rule_weight <= 1.0:
            raise CommandRejected(f"rule_weight out of range: {command.rule_weight}")
        if command.alert_threshold <= 0.0:
            raise CommandRejected("alert_threshold must be positive")
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

    def handle(self, command: ObserveTransaction) -> list[DomainEvent]:
        return self.handle_batch([command])[0]

    def handle_batch(self, commands: Sequence[ObserveTransaction]) -> list[list[DomainEvent]]:
        """Vectorised sibling of :meth:`handle` for bulk ingestion.

        Identical semantics — per-card feature state still advances one transaction
        at a time — but one model call per batch instead of one per transaction.
        """
        for cmd in commands:
            if cmd.amount < 0:
                raise CommandRejected(f"txn {cmd.txn_id}: negative amount {cmd.amount}")
            if not cmd.card_id:
                raise CommandRejected(f"txn {cmd.txn_id}: missing card_id")
        assessments = self.risk.assess_batch([c.as_txn() for c in commands])
        return [self._events_for(c, a) for c, a in zip(commands, assessments)]

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
            raise CommandRejected(f"unknown case {command.case_id!r}")
        if not case.is_open:
            raise CommandRejected(
                f"case {command.case_id} was already dispositioned as {case.disposition!r}"
            )
        if not command.analyst.strip():
            raise CommandRejected("every disposition must name an analyst")
        outcome = Disposition.parse(command.disposition)
        return [
            CaseDispositioned(
                case_id=command.case_id,
                disposition=outcome.value,
                analyst=command.analyst,
                note=command.note,
                decided_at=self.clock.now(),
            )
        ]
