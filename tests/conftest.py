"""Shared fixtures.

One event log is built once per session and shared by every test. Building it costs a
model fit, and the point of the suite is to interrogate *one* log from many angles —
rebuilding it per test would prove nothing extra and blow the runtime budget.

The corpus is deliberately smaller than the demo's (250 cards / 30 days rather than
600 / 45) so the suite stays quick while still carrying enough labelled fraud for the
alert-level assertions to mean something.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.ingest import CorpusConfig, build_event_log, load_corpus
from sentinel.store.sqlite_store import EventStore

#: Fixed for reproducibility; every threshold in the suite was measured at this seed.
SEED = 11
CORPUS = CorpusConfig(n_cards=250, days=30, seed=SEED)


@pytest.fixture(scope="session")
def workspace():
    """A throwaway directory for logs. ``ignore_cleanup_errors`` is required on
    Windows, where a mapped file can briefly outlive its handle."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        yield Path(tmp)


@pytest.fixture(scope="session")
def corpus():
    """The labelled transaction corpus behind the session log."""
    return load_corpus(None, CORPUS)


@pytest.fixture(scope="session")
def session_log(workspace, corpus):
    """A fully built event log plus the read model maintained live during ingestion.

    Yields:
        ``(store, built)`` where ``built.read_model`` is the *live* read model —
        the one the replay tests must reproduce.
    """
    store = EventStore.create_fresh(workspace / "session.db")
    built = build_event_log(corpus, store, seed=SEED)
    try:
        yield store, built
    finally:
        store.close()


@pytest.fixture(scope="session")
def store(session_log):
    return session_log[0]


@pytest.fixture(scope="session")
def built(session_log):
    return session_log[1]


@pytest.fixture(scope="session")
def live_read_model(built):
    """The read model the bus maintained while the log was being written."""
    return built.read_model


@pytest.fixture
def log_copy(store, tmp_path):
    """A private, consistent copy of the session log.

    Tests that write, tamper or seal must not disturb the shared log, otherwise the
    replay-identity assertions would depend on execution order. SQLite's online
    backup gives a consistent snapshot without closing the original.
    """
    target = tmp_path / "copy.db"
    copy = EventStore(target)
    store.connection.backup(copy.connection)
    copy.close()
    reopened = EventStore(target)
    try:
        yield reopened
    finally:
        reopened.close()
