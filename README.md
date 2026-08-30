# FraudShield — an event-sourced card-fraud sentinel

A card-fraud detector whose output is not a score but an **append-only log of facts**.
Every transaction observed, every rule that fired, every score, every alert and every
analyst decision is one immutable, hash-chained record in a SQLite event store. The
analyst queue, the per-card profiles and the daily metrics are all *folds over that
log*, which is what makes the three things below possible at all.

**Audit trail** — the literal ordered subsequence of the log behind one case, with
sequence numbers and chain hashes you can check against the archive yourself:

```
  38,663  TransactionObserved   7dd54c83de8f  C0559 spent 1189.58 on electronics (online) [labelled FRAUD]
  38,664  RuleTriggered         52a7f0ec594c  amount_spike (+0.50) — amount 1189.58 is 6.2 std above card norm
  38,665  RuleTriggered         d4568f832e1a  odd_hour_online_high_value (+0.40) — online 1189.58 at night
  38,666  RiskScored            f8d321d44d61  ml=1.0000 rules=0.90 blended=0.9650 vs threshold 0.238
  38,667  AlertRaised           db56c5eee94a  alert ALERT-00018791 via hybrid, priority 2.1129
  38,668  CaseOpened            1125eb22f72c  case CASE-00018791 queued
  78,043  CaseDispositioned     c500761f7643  a.okafor marked it confirmed_fraud
```

**Time travel** — rebuild every projection over any prefix of the log, so a decision
can be reviewed on the evidence that existed when it was made rather than in hindsight.

**Counterfactual replay** — re-decide the entire log under alternative policies in one
forward pass, using the same `AlertPolicy` object the write side used, so the variant
configured with the recorded policy reproduces the recorded alerts exactly.

## Quickstart

```bash
pip install -r requirements.txt
python -m sentinel demo                  # build, tour the console, prove replay, exit
python -m sentinel dashboard             # live analyst console on the log demo wrote
```

`demo` writes `output/events.db` and needs no arguments. `dashboard` needs a log to
exist first; it loads no model and imports no scikit-learn.

## Operational commands

Everything below takes `--json` and prints one document, so it composes with `jq`.
Anything that returns a list is paged with `--limit` / `--offset`.

```bash
python -m sentinel info                                  # records, head hash, schema, record mix
python -m sentinel cases --status open --min-priority 1  # the queue, filtered and paged
python -m sentinel case --case CASE-00018791             # one case plus its ordered facts
python -m sentinel card --card C0559                     # a card's profile and case history
python -m sentinel stats --since-day 30                  # daily volume, alert rate, outcomes
python -m sentinel replay                                # rebuild projections, print fingerprints
python -m sentinel counterfactual --fp-cost 12 --fn-cost 450
python -m sentinel verify                                # walk the chain, check any seal
python -m sentinel seal                                  # anchor the head (needs FRAUDSHIELD_SEAL_KEY)
python -m sentinel checkpoint                            # save the read model so rebuilds resume
python -m sentinel checkpoint --list                     # what is stored; --prune N keeps the N newest
python -m sentinel export-jsonl --to archive.jsonl       # the grep-able archive
python -m sentinel import-jsonl --from archive.jsonl     # rebuild a log, re-checking every link
```

Failures come back as a typed code, not a traceback: `{"error": "case_not_found", ...}`
on stdout with exit status 2, and a matching JSON line on stderr carrying the
correlation id for that invocation.

## Architecture

The dependency arrow points inward. `domain/` imports nothing from the rest of the
package; `store/` knows the domain; `commands/` knows the store; `query/` and `ui/`
only read.

```
ObserveTransaction (command)
  └─ ObserveTransactionHandler          validate → score → return events
       ├─ FeatureStream                 per-card online state, 12 features, no lookahead
       ├─ FraudModels._blend            0.8 × GradientBoosting + 0.2 × IsolationForest
       ├─ rules.evaluate                weighted rule book, each fire carries a sentence
       └─ AlertPolicy.combine           (1-rule_weight)·ml + rule_weight·rules
  └─ CommandBus                         the single writer
       └─ EventStore.append_groups      SQLite, WAL, synchronous=FULL, one txn per group
            └─ ReadModel.apply          folds the records the STORE returned, not the
                                        handler's output — this is why live == replay
                 ├─ CaseQueueProjection      the analyst queue
                 ├─ CardProfileProjection    per-card rollup
                 ├─ DailyMetricsProjection   volume, alert rate, loss avoided
                 └─ RulePerformanceProjection which rules earn their weight

query/  audit trail · time travel · counterfactual · paged ops queries   (read-only)
ui/     Dashboard.render() — a pure function of state returning list[str]
```

Event vocabulary, in the fixed order they appear within one transaction:
`DetectionPolicyAdopted` (once, at the head) → `TransactionObserved` → `RuleTriggered`\*
→ `RiskScored` → `AlertRaised` → `CaseOpened` → `CaseDispositioned`.

## What the store guarantees

| Guarantee | How | Test |
|---|---|---|
| Append-only | no `UPDATE`/`DELETE` on `events`; the only call that destroys facts is the named `EventStore.create_fresh` (`checkpoint --prune` deletes cached folds, never events) | `test_event_store.py` |
| Durable | WAL + `synchronous=FULL`, one transaction per append group | `test_crash_recovery.py` kills a real child process mid-append |
| Crash-safe | a failed group rolls back whole; a torn JSONL archive tail can be recovered, an edit in the middle cannot | `test_crash_recovery.py` |
| Single order under concurrency | `BEGIN IMMEDIATE` plus `UNIQUE(stream_id, version)` | `test_concurrency.py` races 8 writers on one log |
| Optimistic concurrency | `append_many(..., expected_versions={stream: n})` raises `ConcurrencyConflict` | `test_concurrency.py` |
| Idempotent commands | content-derived `command_id`, `UNIQUE` in `processed_commands`; a repeat returns the first delivery's records | `test_idempotency.py` |
| Tamper-evident | SHA-256 chain over `prev\|seq\|type\|canonical_json(data)` | `test_event_store.py` |
| Tamper-**resistant** | `sentinel seal` MACs the head with a key held outside the database | `test_seal.py` |
| Replayable | live read-model fingerprint == `ReadModel.rebuild(store)`, per projection | `test_replay_identity.py` |
| Checkpoints cannot lie | bound to the chain hash at their own seq, re-fingerprinted after loading, discarded on any mismatch | `test_checkpoints.py` |

The chain alone is only evidence against a careless editor: anyone who can write the
log can edit a payload and recompute every downstream hash, after which `verify()`
passes clean. `test_event_store.py::test_a_fully_recomputed_chain_still_verifies`
asserts exactly that, so nothing in this repo can quietly claim otherwise. The seal is
what closes it, and only while the key lives somewhere the log's writer cannot reach.

## Test

```bash
python -m pytest tests -q                       # the whole suite
python -m pytest tests -q --cov=sentinel        # with coverage
python -m ruff check . && python -m mypy        # lint and types, both configured in pyproject
```

## Measured on this machine

Every number here came from a command in this repo, run on Windows 10 with Python
3.14. Re-run the command and you will get your own.

| Measurement | Value | Command |
|---|---|---|
| Demo end to end | 36.7 s | `python -m sentinel demo`, wall clock around the subprocess |
| Records written | 78,046 | `python -m sentinel --json info` |
| Log size on disk | 45.1 MB | same log, `os.path.getsize` |
| Chain verification | 0.33 s | `python -m sentinel verify` |
| Full projection rebuild | 1.25 s | `ReadModel.rebuild(store)` on that log |
| Rebuild resumed from a checkpoint | 0.09 s | `rebuild_with_checkpoints(store)`, same fingerprint |
| One case's audit trail | 0.4 ms | `case_trail(store, case_id)` on that log |
| Alerts with no rule reason | 143 of 621 (23.0%) | scan of the demo's own `output/events.db` |
| Test suite | 221 tests, 92% line coverage | `python -m pytest tests -q --cov=sentinel` |

Detection quality on the demo corpus, from `output/run_report.md` written by
`python -m sentinel demo`: AUC 1.0 and average precision 0.9973 on the temporal
holdout; at the alert level over the whole stream, precision 0.799 and recall 1.0
(496 TP / 125 FP / 0 FN).

**Read those two detection numbers as a property of the generator, not of the
detector.** See below.

## Limitations

Stated plainly, because the rest of this README is only worth reading if this section is.

- **The corpus is synthetic.** `sentinel/detection/synthetic.py` injects three fraud
  signatures that the twelve features separate almost perfectly, which is why AUC comes
  out at 1.0. That figure measures the generator. Any claim about real-world accuracy
  would need a real transaction export, and there is not one here.
- **Nothing is deployed.** There is no service, no endpoint, no live URL, and no
  production usage. This is a reference implementation you run locally.
- **Nothing blocks a transaction.** The system observes, scores, alerts and opens a
  case. There is no decline path and no issuer integration.
- **The rules layer does not pay for itself on this corpus.** The demo's own
  counterfactual table prints it: at $12/FP and $450/FN the recorded policy costs
  $1,500 against $510 for "model only (rules muted)". The rules are kept for
  explainability — 77% of alerts carry a human-readable reason because of them — not
  because they improve the cost. On a real corpus this trade-off would need re-deciding.
- **23% of alerts carry no reason.** The model alerted and no rule fired; the console
  says "model-only alert" rather than inventing one.
- **The seal is only as good as where the key lives.** Held next to the log it proves
  nothing. An attacker with write access can also delete seal rows — visibly, which is
  why `verify --require-seal` fails when there are none.
- **Projections are not memory-bounded.** `CaseQueueProjection` keeps every case and
  `CardProfileProjection` every card for the life of the process. The store streams,
  the folds do not. A log far larger than memory replays but the read model will not fit.
- **Checkpoints are not written automatically.** `sentinel checkpoint` saves the read
  model so later rebuilds resume instead of folding from record 1, but nothing calls
  it for you — there is no policy that decides when one is due. Without a checkpoint,
  rebuilding as of sequence *n* folds *n* records, ~1.25 s at 78k.
- **Single-machine only.** One SQLite file, one host. The concurrency guarantees are
  SQLite's, and they stop at the filesystem.

## Configuration

Copy `.env.example`. Nothing is read automatically; export what you need.

| Variable | Purpose |
|---|---|
| `FRAUDSHIELD_SEAL_KEY` | key for `sentinel seal` / `verify`. Must live outside the log. |
| `FRAUDSHIELD_LOG_LEVEL` | verbosity of the JSON log lines on stderr. Default `WARNING`. |

There are no secrets in this repository and none are needed to run it.

---

## Author & Contact

- **Author**: Nathaniel Gordon
- **Role**: Senior AI & Machine Learning Engineer
- **GitHub**: [github.com/nathaniel-gordon](https://github.com/nathaniel-gordon)
- **Portfolio / Upwork**: [upwork.com/freelancers/~015fe5a704f8943797](https://www.upwork.com/freelancers/~015fe5a704f8943797)
- **Email**: nathanielgordon346@gmail.com
- **Location**: Tallahassee, FL, USA
