"""The command line, exercised end to end against a small real log.

One tiny corpus is built once, then every read-only subcommand runs against it in
both human and ``--json`` mode. The point is not the formatting: it is that the
operational commands exist, exit 0, and emit JSON a script can parse — and that a
failure comes back as a typed code and exit 2 rather than a traceback.
"""
from __future__ import annotations

import json

import pytest

from sentinel.__main__ import main
from sentinel.store.seal import ENV_KEY

CARDS, DAYS = 40, 8


@pytest.fixture(scope="module")
def cli_log(tmp_path_factory):
    """A small log built through the CLI itself."""
    path = tmp_path_factory.mktemp("cli") / "events.db"
    assert main(["--log", str(path), "build", "--cards", str(CARDS), "--days", str(DAYS)]) == 0
    return str(path)


def run(capsys, argv: list[str]) -> tuple[int, str]:
    code = main(argv)
    return code, capsys.readouterr().out


def run_json(capsys, argv: list[str]):
    code, out = run(capsys, argv)
    assert code == 0, out
    return json.loads(out)


def test_build_created_a_verifiable_log(capsys, cli_log):
    payload = run_json(capsys, ["--log", cli_log, "--json", "verify"])
    assert payload["chain"] == "ok"
    assert payload["records_verified"] > 0
    assert len(payload["head_hash"]) == 64


def test_info_describes_the_log(capsys, cli_log):
    payload = run_json(capsys, ["--log", cli_log, "--json", "info"])
    assert payload["type_counts"]["DetectionPolicyAdopted"] == 1
    assert payload["commands_applied"] > 0
    assert payload["schema_version"] >= 2
    assert payload["seals"] == []

    code, text = run(capsys, ["--log", cli_log, "info"])
    assert code == 0 and "record mix" in text


def test_replay_reports_stable_fingerprints(capsys, cli_log):
    payload = run_json(capsys, ["--log", cli_log, "--json", "replay"])
    assert payload["stable_across_rebuilds"] is True
    assert set(payload["fingerprints"]) == {
        "card_profiles", "case_queue", "daily_metrics", "rule_performance"
    }


def test_cases_are_paged(capsys, cli_log):
    first = run_json(capsys, ["--log", cli_log, "--json", "cases", "--limit", "3"])
    assert len(first["items"]) == 3
    assert first["next_offset"] == 3
    second = run_json(
        capsys, ["--log", cli_log, "--json", "cases", "--limit", "3", "--offset", "3"]
    )
    assert {c["case_id"] for c in first["items"]} != {c["case_id"] for c in second["items"]}

    code, text = run(capsys, ["--log", cli_log, "cases", "--limit", "3"])
    assert code == 0 and "CASE_ID" in text


def test_case_and_card_and_stats(capsys, cli_log):
    listing = run_json(capsys, ["--log", cli_log, "--json", "cases", "--limit", "1"])
    case_id = listing["items"][0]["case_id"]
    card_id = listing["items"][0]["card_id"]

    detail = run_json(capsys, ["--log", cli_log, "--json", "case", "--case", case_id])
    assert detail["case"]["case_id"] == case_id
    assert detail["trail"][0]["type"] == "TransactionObserved"

    card = run_json(capsys, ["--log", cli_log, "--json", "card", "--card", card_id])
    assert card["card_id"] == card_id
    assert card["profile"]["txns"] >= 1

    report = run_json(capsys, ["--log", cli_log, "--json", "stats"])
    assert report["totals"]["txns"] > 0
    assert len(report["days"]) <= DAYS + 1


def test_audit_prints_the_trail(capsys, cli_log):
    listing = run_json(capsys, ["--log", cli_log, "--json", "cases", "--limit", "1"])
    case_id = listing["items"][0]["case_id"]
    trail = run_json(capsys, ["--log", cli_log, "--json", "audit", "--case", case_id])
    assert [entry["type"] for entry in trail][0] == "TransactionObserved"


def test_the_counterfactual_costs_follow_the_cost_flags(capsys, cli_log):
    cheap = run_json(capsys, ["--log", cli_log, "--json", "--fp-cost", "1", "--fn-cost", "1",
                              "counterfactual"])
    dear = run_json(capsys, ["--log", cli_log, "--json", "--fp-cost", "100", "--fn-cost", "100",
                             "counterfactual"])
    assert cheap["costs"] == {"fp_cost": 1.0, "fn_cost": 1.0}
    assert "baseline (as recorded)" in cheap["reproduce_log"]
    for lo, hi in zip(cheap["variants"], dear["variants"]):
        assert hi["cost"] == pytest.approx(lo["cost"] * 100)


def test_export_and_import_round_trip_to_the_same_head(capsys, cli_log, tmp_path):
    archive = tmp_path / "archive.jsonl"
    exported = run_json(capsys, ["--log", cli_log, "--json", "export-jsonl", "--to", str(archive)])
    assert archive.exists()

    restored_path = tmp_path / "restored.db"
    restored = run_json(
        capsys,
        ["--log", str(restored_path), "--json", "import-jsonl", "--from", str(archive)],
    )
    assert restored["imported"] == exported["exported"]
    assert restored["head_hash"] == exported["head_hash"]


def test_seal_then_verify(capsys, cli_log, monkeypatch):
    monkeypatch.setenv(ENV_KEY, "a-test-key")
    sealed = run_json(capsys, ["--log", cli_log, "--json", "seal"])
    assert len(sealed["mac"]) == 64

    checked = run_json(capsys, ["--log", cli_log, "--json", "verify", "--require-seal"])
    assert checked["seal"]["ok"] is True
    assert checked["seal"]["seals_checked"] == 1


def test_a_missing_seal_key_fails_with_a_code_not_a_traceback(capsys, cli_log, monkeypatch):
    monkeypatch.delenv(ENV_KEY, raising=False)
    code, out = run(capsys, ["--log", cli_log, "--json", "verify", "--require-seal"])
    assert code == 2
    payload = json.loads(out)
    assert payload["error"] == "seal_mismatch"
    assert payload["correlation_id"]


def test_a_remote_log_path_is_refused(capsys):
    code, out = run(capsys, ["--log", "https://example.invalid/events.db", "--json", "info"])
    assert code == 2
    assert json.loads(out)["error"] == "invalid_path"


def test_a_missing_case_is_a_typed_error(capsys, cli_log):
    code, out = run(capsys, ["--log", cli_log, "--json", "case", "--case", "CASE-99999999"])
    assert code == 2
    assert json.loads(out)["error"] == "case_not_found"


def test_bad_paging_is_a_typed_error(capsys, cli_log):
    code, out = run(capsys, ["--log", cli_log, "--json", "cases", "--limit", "0"])
    assert code == 2
    assert json.loads(out)["error"] == "validation_failed"


# ------------------------------------------------------------- checkpoints


def test_checkpoint_saves_lists_and_prunes(capsys, tmp_path_factory):
    """The subcommand's three modes, on a log of its own so ordering cannot matter."""
    path = str(tmp_path_factory.mktemp("cp") / "events.db")
    assert main(["--log", path, "build", "--cards", "20", "--days", "5"]) == 0
    capsys.readouterr()  # drop the build chatter; only the json below is read

    empty = run_json(capsys, ["--log", path, "--json", "checkpoint", "--list"])
    assert empty == {"checkpoints": [], "total": 0}

    saved = run_json(capsys, ["--log", path, "--json", "checkpoint"])
    assert saved["applied"] > 0
    assert saved["names"] == ["card_profiles", "case_queue", "daily_metrics", "rule_performance"]
    assert len(saved["fingerprint"]) == 64

    listed = run_json(capsys, ["--log", path, "--json", "checkpoint", "--list"])
    assert listed["total"] == 1
    assert listed["checkpoints"][0]["seq"] == saved["seq"]

    pruned = run_json(capsys, ["--log", path, "--json", "checkpoint", "--prune", "0"])
    assert pruned == {"pruned": 1, "remaining": 0}


def test_replay_agrees_with_itself_after_a_checkpoint(capsys, tmp_path_factory):
    """A stored checkpoint must not change a single fingerprint the CLI reports."""
    path = str(tmp_path_factory.mktemp("cpreplay") / "events.db")
    assert main(["--log", path, "build", "--cards", "20", "--days", "5"]) == 0
    capsys.readouterr()

    before = run_json(capsys, ["--log", path, "--json", "replay"])
    run_json(capsys, ["--log", path, "--json", "checkpoint"])
    after = run_json(capsys, ["--log", path, "--json", "replay"])

    assert after["fingerprints"] == before["fingerprints"]
    assert after["head_hash"] == before["head_hash"]


def test_checkpoint_human_output_mentions_the_sequence(capsys, cli_log):
    code, text = run(capsys, ["--log", cli_log, "checkpoint"])
    assert code == 0 and "saved the read model" in text
    code, text = run(capsys, ["--log", cli_log, "checkpoint", "--list"])
    assert code == 0 and "fingerprint" in text
    assert run(capsys, ["--log", cli_log, "checkpoint", "--prune", "0"])[0] == 0
