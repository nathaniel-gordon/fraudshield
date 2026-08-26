# FraudShield — Real-Time Transaction Fraud Defense

> Stop fraud before it clears. FraudShield combines three detection layers — a supervised gradient-boosting classifier, an IsolationForest anomaly detector for novel patterns, and an explainable rules engine — in a streaming sentinel that maintains per-card online state and feeds a priority-ranked case queue where every alert carries human-readable reasons.

## What FraudShield Does

- **Three-layer detection** — ML classifier + anomaly detector + explainable rules, blended
- **Per-card streaming state** — online velocity, spend norms, txn frequency; no lookahead leakage
- **Temporal evaluation** — train on first 70% of timeline, test on last 30%
- **Cost-tuned threshold** — minimizes `FP x analyst_cost + FN x fraud_loss` (configurable)
- **CQRS + Event Sourcing** — full append-only audit log; time-travel and counterfactual replay

## Architecture

```
Transaction Stream
    └─> FeatureStream       (per-card rolling state)
        ├─> GradientBoosting (ML classifier)
        ├─> IsolationForest  (anomaly detection)
        └─> RulesEngine      (travel, burst, takeover rules)
    └─> ScoreBlender        (0.65 x ML + 0.35 x rules)
    └─> AlertQueue          (priority-ranked, human-readable)
    └─> EventStore (JSONL)  (CQRS audit log, time-travel)
    └─> TUI Dashboard       (analyst console)
```

## Quickstart

```bash
python -m sentinel demo        # generate 40k txns, train, stream, write case queue
python -m sentinel dashboard   # open live analyst TUI
```

## Test

```bash
python tests/test_smoke.py
```

---

## 👤 Author & Contact

- **Author**: Nathaniel Gordon
- **Role**: Senior AI & Machine Learning Engineer
- **GitHub**: [github.com/nathaniel-gordon](https://github.com/nathaniel-gordon)
- **Portfolio / Upwork**: [upwork.com/freelancers/~015fe5a704f8943797](https://www.upwork.com/freelancers/~015fe5a704f8943797)
- **Email**: nathanielgordon346@gmail.com
- **Location**: Tallahassee, FL, USA
