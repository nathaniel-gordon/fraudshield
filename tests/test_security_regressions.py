"""One pinned test per security hole the codebase used to have.

Each of these was a real defect that got fixed: the dashboard deserialised a joblib
pickle from beside the log, ``--data`` would fetch a URL because pandas accepts one,
and identifiers flowed toward SQL. The unit tests for the fixes live with their
modules; this file exists so a regression is reported as what it is — a security
regression — rather than as one red assertion among many.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from sentinel.__main__ import main
from sentinel.query.audit import case_trail
from sentinel.store.seal import ENV_KEY

REPO = Path(__file__).resolve().parents[1]


# ------------------------------------------------- the pickle hole (dashboard)


def test_no_module_imports_a_pickle_loader():
    """The fix removed ``joblib.load`` from the read side; nothing may bring it back.

    The model is fitted, used, and dropped — every score it ever produced is on the
    log. Any ``import pickle`` or ``import joblib`` in the package would reopen an
    arbitrary-code-execution path on a file an attacker can place next to the log.
    """
    pattern = re.compile(
        r"^\s*(import\s+(joblib|pickle)\b|from\s+(joblib|pickle)\b)", re.MULTILINE
    )
    offenders = [
        str(path)
        for path in (REPO / "sentinel").rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_query_side_runs_without_sklearn_or_joblib(log_copy):
    """Opening a log and serving the queue must not import the ML stack at all.

    Run in a fresh interpreter, because this process already loaded scikit-learn to
    build the session log. If ``sklearn`` or ``joblib`` appears in ``sys.modules``
    after a query, something on the read path started loading a model again.
    """
    log_copy.close()
    script = (
        "import sys\n"
        "from sentinel.__main__ import main\n"
        f"rc = main(['--log', {str(log_copy.path)!r}, '--json', 'cases', '--limit', '1'])\n"
        "assert rc == 0, rc\n"
        "bad = [m for m in ('joblib', 'sklearn') if m in sys.modules]\n"
        "assert not bad, f'query side imported {bad}'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_a_planted_model_file_is_ignored(capsys, log_copy):
    """A hostile ``detector.joblib`` beside the log must stay exactly as planted."""
    log_copy.close()
    planted = log_copy.path.parent / "detector.joblib"
    payload = b"\x80\x04this would explode if anything unpickled it"
    planted.write_bytes(payload)
    assert main(["--log", str(log_copy.path), "--json", "cases", "--limit", "1"]) == 0
    capsys.readouterr()
    assert planted.read_bytes() == payload  # untouched, unparsed


# ------------------------------------------ the outbound fetch (--data / --log)


@pytest.mark.parametrize("argv", [
    ["--log", "https://evil.example/events.db", "--json", "info"],
    ["--log", "s3://bucket/events.db", "--json", "verify"],
])
def test_a_remote_log_location_is_refused_end_to_end(capsys, argv):
    assert main(argv) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["error"] == "invalid_path"


def test_a_remote_data_url_is_refused_before_pandas_sees_it(capsys, tmp_path):
    code = main([
        "--log", str(tmp_path / "x.db"), "--json",
        "build", "--data", "https://evil.example/corpus.csv", "--cards", "5", "--days", "2",
    ])
    assert code == 2
    last_line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(last_line)["error"] == "invalid_path"
    assert not (tmp_path / "x.db").exists()  # refused before anything was written


# --------------------------------------------------------- identifier handling


def test_hostile_case_and_card_ids_cannot_reach_sql(log_copy):
    """Lookups are parameterised; an id is data even when it looks like SQL."""
    hostile = "CASE-1' OR '1'='1'; DROP TABLE events; --"
    before = log_copy.count()
    assert log_copy.records_for_case(hostile) == []
    assert log_copy.seq_of_case(hostile) is None
    assert case_trail(log_copy, hostile) == []
    assert log_copy.stream_version(f"card:{hostile}") == 0
    assert log_copy.count() == before  # the table is still there, same size


def test_a_hostile_case_id_via_the_cli_is_a_clean_miss(capsys, log_copy):
    log_copy.close()
    with pytest.raises(SystemExit, match="not present"):
        main(["--log", str(log_copy.path), "--json", "audit",
              "--case", "x'; DELETE FROM events; --"])
    capsys.readouterr()
    assert main(["--log", str(log_copy.path), "--json", "verify"]) == 0
    assert json.loads(capsys.readouterr().out)["chain"] == "ok"


# ------------------------------------------------------------- the seal's key


def test_the_seal_key_never_appears_in_any_output(capsys, monkeypatch, log_copy):
    """The MAC and the key id are derived; the key itself must never be printed."""
    log_copy.close()
    key = "deadbeef" * 8
    monkeypatch.setenv(ENV_KEY, key)
    assert main(["--log", str(log_copy.path), "--json", "seal"]) == 0
    sealed = capsys.readouterr()
    assert main(["--log", str(log_copy.path), "--json", "verify", "--require-seal"]) == 0
    verified = capsys.readouterr()
    for text in (sealed.out, sealed.err, verified.out, verified.err):
        assert key not in text
