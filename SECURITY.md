# Security

## Reporting

Email nathanielgordon346@gmail.com with a description and reproduction steps.
Please do not open a public issue for anything exploitable; you will get a reply
before any fix is discussed publicly. There is no bounty programme.

## Threat model, honestly stated

This is a local, offline tool. Its security story is about the **integrity of the
event log as evidence**, not about network attackers — there is no listener, no
authentication layer, and no multi-user model.

What the design defends:

- **Log integrity against editing.** Every record is SHA-256-chained; `verify`
  walks the chain. Because a writer can recompute the chain, `seal` additionally
  anchors the head with `HMAC-SHA256` under a key the writer must not hold
  (`FRAUDSHIELD_SEAL_KEY`). The suite proves both directions: tampering is caught,
  and a *fully recomputed* chain passes the walk — which is exactly why the seal
  exists.
- **No deserialisation of executable content.** No pickle, no joblib anywhere in
  the package; checkpoints are JSON and are discarded on any fingerprint mismatch.
  A regression test plants a hostile `detector.joblib` beside the log and checks it
  stays unread.
- **No outbound requests.** Path arguments are resolved and scheme-checked at the
  edge; `https://` / `s3://` locations are refused before pandas or sqlite see
  them.
- **Injection.** All SQL is parameterised; hostile case/card identifiers are data.
- **Secrets and PII in logs.** The seal key never appears in any output (a derived
  key id does); card identifiers are masked in log lines; amounts and analyst notes
  are not logged.

What it does **not** defend:

- An attacker with write access to the database file can delete records or seal
  rows. Deletion is *visible* (counts, `verify --require-seal`), not preventable.
- A seal key stored on the same host as the log proves nothing.
- The container runs as a non-root user, but anyone who can run `docker` on the
  host effectively owns the volume.

## Supported versions

There are no releases yet; report against `main`.

## Dependencies

Runtime dependencies are numpy, pandas and scikit-learn (scikit-learn is imported
only when a model is fitted). Dependabot watches pip, GitHub Actions and the Docker
base image weekly.
