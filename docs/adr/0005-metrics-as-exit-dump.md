# 0005 — Metrics are per-run, dumped at exit, not served

Status: accepted

## Context

The delivery pass needed an observability surface: counters, latency histograms,
health. Every `python -m sentinel` invocation is a short-lived process; the only
long-lived mode is the interactive console, which owns the terminal.

## Problem

The standard shape — an HTTP `/metrics` endpoint — assumes a resident process for a
scraper to visit. Bolting a server thread onto a batch CLI would mean inventing a
daemon this system does not otherwise need, and the numbers would vanish with the
process anyway.

## Options considered

- **HTTP endpoint (`http.server` thread).** Serves nothing most of its life;
  scrapers would race process exit; adds a port and a threat surface to a tool whose
  security story is "no network listener".
- **Pushgateway-style write to a file per run.** Close, but invents a storage
  format and a retention question the operator's own job runner already answers.
- **In-process registry + one JSON document on stderr at exit (`--metrics`).**
  The run's numbers land next to the run's structured log lines, in whatever
  captures job output.

## Decision

`sentinel/metrics.py`: a registry of named counters and bucketed latency
histograms, written into by the store (`store.events_appended`, `store.append_ms`,
`store.verify_ms`), the bus (`bus.commands_accepted` / `_duplicate` / `_rejected`,
`bus.ingest_ms`) and the rebuild path (`rebuild.checkpoint_hit` / `_miss`,
`rebuild.replay_ms`). `--metrics` dumps one snapshot on stderr; stdout stays a
single parseable document under `--json`. Liveness is a separate concern and got
its own subcommand (`health`, exit 0/1) so a container HEALTHCHECK and a cron
wrapper have a contract that never exits 2.

## Consequences

- No daemon, no port, no scrape config; a pipeline gets metrics with
  `2>> run.jsonl`.
- Counters are cumulative *within one process only*; cross-run aggregation is the
  job runner's problem, deliberately.
- If a resident process ever appears, the registry's snapshot (cumulative counters,
  cumulative buckets) is already the data model an exporter would serve.
