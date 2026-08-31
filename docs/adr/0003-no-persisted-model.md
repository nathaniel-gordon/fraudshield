# 0003 — The fitted model is never written to disk

Status: accepted

## Context

`build` fits a scikit-learn ensemble, scores every transaction, and records each
score as a `RiskScored` event. An earlier version also saved the fitted model as
`detector.joblib` next to the log, and the dashboard loaded it back on open.

## Problem

A joblib file is a pickle, and unpickling is arbitrary code execution. A file
sitting beside the log, loaded automatically by a viewer-shaped command, is exactly
the payload an attacker with filesystem access would swap. And nothing actually
needed it: every score the model ever produced is already on the log.

## Options considered

- **Keep joblib, add a checksum.** A checksum written by the same host the attacker
  controls verifies nothing.
- **Export to a non-executable format (ONNX / JSON coefficients).** Safe, but adds a
  conversion dependency and keeps alive the idea that the read side re-scores —
  which would break replay identity, since a re-scored decision could differ from
  the recorded one.
- **Do not persist the model at all.** The log is the interface between write and
  read sides; the read side replays recorded facts and never scores.

## Decision

No model persistence. `FraudModels` has deliberately no save/load. The read side
(replay, audit, time travel, counterfactual, dashboard, health) runs without
scikit-learn — enforced at import level: sklearn is imported inside the fitting
methods only, and `test_security_regressions.py` runs a query in a fresh interpreter
and fails if `sklearn` or `joblib` appears in `sys.modules`.

## Consequences

- Re-scoring new data requires re-fitting (`build` on a fresh log). That is the
  trade: reproducibility and safety over incremental model reuse.
- Counterfactual replay re-decides from *recorded* scores under different policies;
  it cannot explore "what if the model were different", only "what if the policy
  were".
- A planted `detector.joblib` is inert; a regression test plants one and checks it
  stays unread.
