# Contributing

## Setup

```bash
git clone <your fork>
cd fraudshield
python -m venv .venv && . .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements.txt
pip install pytest pytest-cov ruff mypy
```

Python ≥ 3.11. No compilers needed; everything installs from wheels. The project is
developed on Windows and tested on Windows and Linux in CI — keep both working.

## Running things

```bash
python -m sentinel demo            # end-to-end tour, writes output/events.db
python -m sentinel dashboard       # interactive console on that log
python -m pytest tests             # the suite (~2 minutes; one shared log per session)
python -m pytest tests --cov=sentinel
python -m ruff check .
python -m mypy
```

CI runs exactly those commands plus a Docker build; all of them must pass before a
change is mergeable. There are no other gates.

## Architecture in one paragraph

Commands go through `CommandBus` (the single writer) to handlers that return
events; `EventStore` appends them to a hash-chained SQLite log; every read surface —
queue, profiles, metrics, audit, time travel, counterfactual — is a fold over that
log. The dependency arrow points inward: `domain/` imports nothing from the package,
`store/` knows the domain, `commands/` knows the store, `query/` and `ui/` only
read. The README's architecture section has the full diagram, and `docs/adr/`
records why the load-bearing decisions went the way they did — read those before
proposing to reverse one.

## Rules the suite will hold you to

- **The log is append-only.** No `UPDATE`/`DELETE` on `events` anywhere; a test
  greps the source for mutating SQL.
- **Live == replay.** The read model folds records the *store* returned, never a
  handler's output. If your change makes a projection depend on anything but the
  record stream, `test_replay_identity` will fail — that is it working.
- **No pickle, no joblib, anywhere.** The read side must run without scikit-learn;
  `test_security_regressions.py` checks both in a fresh interpreter.
- **Paths are resolved at the edge** (`sentinel/paths.py`); user input never
  reaches `pandas.read_csv` or `sqlite3.connect` raw.
- **Every list is paged, everything takes `--json`,** and errors leave as typed
  codes with exit 2 (`health` alone exits 0/1 — it is a probe).

## Coding standards

`ruff` and `mypy` configuration lives in `pyproject.toml` and is the standard —
don't argue with the linter in a PR, change the config in its own commit with a
reason. Beyond that: docstrings explain *why* (the constraint, the failure mode
that motivated the code), not what the next ten lines do; measured numbers in docs
must come from a command a reader can rerun.

## Tests

New behaviour comes with tests in the same PR. The suite builds one small corpus
per session (`tests/conftest.py`); tests that tamper or write take a `log_copy`,
never the shared store. If your test needs a different corpus shape, build a tiny
log through the CLI the way `test_cli.py` does rather than growing the shared one.

## Commits

Small, real steps — one change per commit, subject under ~60 characters, imperative
mood. A body only when the why isn't obvious from the diff. Don't push generated
files (`output/`, `.coverage`, caches); `.gitignore` already covers them.
