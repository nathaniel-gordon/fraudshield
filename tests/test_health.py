"""The health probe: a plain healthy/unhealthy answer, never a traceback.

``health`` exists for a container HEALTHCHECK and a cron wrapper, so its contract
is narrower than the other subcommands': exit 0 when the log can be served, exit 1
for every possible way it cannot — absent, empty, unreadable, torn or tampered.
Nothing in here may surface as exit 2 or an unhandled exception, because a probe
that crashes reads as "unknown", not "down".
"""
from __future__ import annotations

import json

from sentinel.__main__ import main
from sentinel.store.sqlite_store import EventStore


def probe(capsys, log: str, *extra: str):
    code = main(["--log", log, "--json", "health", *extra])
    return code, json.loads(capsys.readouterr().out)


def test_a_built_log_is_healthy(capsys, log_copy):
    log_copy.close()
    code, doc = probe(capsys, str(log_copy.path))
    assert code == 0
    assert doc["status"] == "ok"
    assert doc["checks"]["contiguous"] is True
    assert doc["checks"]["self_describing"] is True
    assert doc["checks"]["records"] > 0


def test_deep_mode_verifies_the_whole_chain(capsys, log_copy):
    log_copy.close()
    code, doc = probe(capsys, str(log_copy.path), "--deep")
    assert code == 0
    assert doc["checks"]["chain_verified"] is True


def test_a_missing_log_is_unhealthy_not_an_error(capsys, tmp_path):
    code, doc = probe(capsys, str(tmp_path / "nowhere.db"))
    assert code == 1
    assert doc["status"] == "fail"
    assert doc["checks"]["openable"] is False


def test_an_empty_log_is_not_ready(capsys, tmp_path):
    EventStore.create_fresh(tmp_path / "empty.db").close()
    code, doc = probe(capsys, str(tmp_path / "empty.db"))
    assert code == 1
    assert doc["checks"]["non_empty"] is False


def test_a_file_that_is_not_a_database_is_unhealthy(capsys, tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a sqlite file, whatever the extension says")
    code, doc = probe(capsys, str(junk))
    assert code == 1
    assert doc["checks"]["openable"] is False


def test_a_tampered_payload_passes_shallow_and_fails_deep(capsys, log_copy):
    """The shallow probe is O(1) on purpose; --deep is the one that re-hashes."""
    log_copy.connection.execute(
        "UPDATE events SET data = replace(data, '\"amount\":', '\"amount_\":')"
        " WHERE seq = (SELECT MAX(seq) FROM events WHERE type = 'TransactionObserved')"
    )
    log_copy.connection.commit()
    log_copy.close()

    code, _ = probe(capsys, str(log_copy.path))
    assert code == 0  # head, count and header are all still intact

    code, doc = probe(capsys, str(log_copy.path), "--deep")
    assert code == 1
    assert "chain" in doc["checks"].get("detail", "").lower() or (
        doc["checks"].get("chain_verified") is not True
    )


def test_the_human_output_says_ok_or_fail(capsys, log_copy):
    log_copy.close()
    assert main(["--log", str(log_copy.path), "health"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ok:")
    assert "contiguous" in out
