# Fraud Sentinel — event-sourced run report

Event log `smoke.jsonl`: 7,162 records, chain head `833561f4d215f6e0`.

## Detection quality

- model on the temporal holdout: AUC **1.0**, average precision **1.0**, cost-tuned threshold **0.189**
- alert level over the whole stream: precision **0.667**, recall **0.978** (44 TP / 22 FP / 1 FN)
- policy in force: threshold 0.189, rule weight 0.35, rules-only floor 0.85

## Read-model fingerprints (rebuilt purely by replay)

- `card_profiles` — `44e85f38f088e417ab4d228bd743ed7bd91f2cb23990e6d07ed8694922d1f0fd`
- `case_queue` — `7701ed0580e32f37d78af111037385d4cb3a15296e6e1baefea207c3e3f0be07`
- `daily_metrics` — `8b4f5fa6937549c3099afb0d67276a0d4abc5e25d31aef023c09d9d984965a88`
- `rule_performance` — `f682e57a644188f2ef64fa56fece525106459cb98eb2d80124831a3fefd8a8c8`

## Rule performance

| rule | fires | on labelled fraud | label precision | closed | analyst precision |
|---|---:|---:|---:|---:|---:|
| amount_spike | 35 | 15 | 0.4286 | 1 | 0.0000 |
| odd_hour_online_high_value | 10 | 10 | 1.0000 | 3 | 0.5000 |
| new_category_high_value | 5 | 2 | 0.4000 | 0 | 0.0000 |
| impossible_travel | 4 | 3 | 0.7500 | 1 | 0.0000 |
| rapid_fire | 3 | 3 | 1.0000 | 1 | 1.0000 |

## Counterfactual policy replay

| variant | alerts | tp | fp | fn | precision | recall | cost $ |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline (as recorded) | 66 | 44 | 22 | 1 | 0.6667 | 0.9778 | 714 |
| stricter cut (2x threshold) | 46 | 44 | 2 | 1 | 0.9565 | 0.9778 | 474 |
| looser cut (0.5x threshold) | 127 | 45 | 82 | 0 | 0.3543 | 1.0000 | 984 |
| rules-led blend (rw 0.60) | 67 | 44 | 23 | 1 | 0.6567 | 0.9778 | 726 |
| model only (rules muted) | 51 | 45 | 6 | 0 | 0.8824 | 1.0000 | 72 |
| impossible_travel disabled | 65 | 44 | 21 | 1 | 0.6769 | 0.9778 | 702 |
| impossible_travel de-weighted 0.9->0.5 | 66 | 44 | 22 | 1 | 0.6667 | 0.9778 | 714 |

## Audit trail — CASE-00002099

```
    4325  TransactionObserved    b0b957e919db  C0091 spent 1190.56 on online_retail (online) at t+1,049,438s [labelled FRAUD]
    4326  RuleTriggered          1cc1346c5d49  odd_hour_online_high_value (+0.40) — online 1190.56 at night, 9.1x usual spend
    4327  RiskScored             44c847ea979a  ml=1.0000 rules=0.40 blended=0.7900 vs threshold 0.189 (rule weight 0.35)
    4328  AlertRaised            4e71156fe1a7  alert ALERT-00002099 via ml_blend, priority 1.7305
    4329  CaseOpened             7148c2acf04d  case CASE-00002099 queued — C0091 1190.56 online_retail :: odd_hour_online_high_value
    7159  CaseDispositioned      658e06b04dcc  a.okafor marked it confirmed_fraud — confirmed fraud — card blocked
```
