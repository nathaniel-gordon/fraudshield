"""The two modules that sit at the edge: path handling and structured logging.

``paths`` exists because ``pandas.read_csv`` will happily fetch ``https://`` and
``s3://``, which turns a ``--data`` argument into an outbound request. This tool reads
local files. ``obs`` exists so an operator can correlate a CLI invocation with the
commands it submitted, and so that a card number never ends up in an ops pipeline.
"""
from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from sentinel.errors import InvalidPath
from sentinel.obs import (
    JsonFormatter,
    Timer,
    configure_logging,
    correlation_id,
    correlation_scope,
    get_logger,
    mask_card,
    new_correlation_id,
)
from sentinel.paths import input_file, normalize, output_file


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.invalid/transactions.csv",
        "http://example.invalid/x.csv",
        "s3://bucket/key.csv",
        "gs://bucket/key.csv",
        "ftp://host/file.csv",
    ],
)
def test_remote_locations_are_refused(raw):
    with pytest.raises(InvalidPath, match="remote locations"):
        normalize(raw)


def test_a_windows_drive_letter_is_not_a_url_scheme(tmp_path):
    """The scheme pattern needs two or more characters, or C:\\ would look remote."""
    target = tmp_path / "data.csv"
    target.write_text("txn_id\n", encoding="utf-8")
    assert normalize(str(target)) == target.resolve()


def test_paths_are_resolved_once_at_the_edge(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    messy = nested / ".." / ".." / "log.db"
    assert normalize(str(messy)) == (tmp_path / "log.db").resolve()
    assert normalize(str(messy)).is_absolute()


def test_an_input_file_must_exist_and_be_a_file(tmp_path):
    missing = tmp_path / "nope.csv"
    with pytest.raises(InvalidPath, match="does not exist"):
        input_file(missing)

    directory = tmp_path / "adir"
    directory.mkdir()
    with pytest.raises(InvalidPath, match="not a regular file"):
        input_file(directory)

    real = tmp_path / "ok.csv"
    real.write_text("x\n", encoding="utf-8")
    assert input_file(real) == real.resolve()


def test_an_output_path_gets_its_parent_and_refuses_a_directory(tmp_path):
    target = output_file(tmp_path / "deep" / "nest" / "log.db")
    assert target.parent.is_dir()

    with pytest.raises(InvalidPath, match="is a directory"):
        output_file(tmp_path / "deep")


def test_invalid_path_errors_carry_the_offending_path(tmp_path):
    with pytest.raises(InvalidPath) as caught:
        input_file(tmp_path / "gone.csv")
    payload = caught.value.as_dict()
    assert payload["error"] == "invalid_path"
    assert "gone.csv" in payload["details"]["path"]


# ------------------------------------------------------------------ logging


def test_every_line_is_one_json_object_with_the_correlation_id():
    stream = StringIO()
    configure_logging("INFO", stream=stream)
    log = get_logger("sentinel.test")
    with correlation_scope("fixed-id"):
        log.info("something_happened", extra={"records": 3})

    line = stream.getvalue().strip()
    payload = json.loads(line)
    assert payload["event"] == "something_happened"
    assert payload["correlation_id"] == "fixed-id"
    assert payload["records"] == 3
    assert payload["level"] == "INFO"
    assert "\n" not in line


def test_the_correlation_id_is_scoped_and_restored():
    assert correlation_id() == "-"
    with correlation_scope("outer"):
        assert correlation_id() == "outer"
        with correlation_scope("inner"):
            assert correlation_id() == "inner"
        assert correlation_id() == "outer"
    assert correlation_id() == "-"


def test_a_generated_id_is_short_and_unique():
    ids = {new_correlation_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 16 for i in ids)


def test_configuring_twice_on_one_stream_does_not_double_up():
    stream = StringIO()
    configure_logging("INFO", stream=stream)
    configure_logging("INFO", stream=stream)
    get_logger("sentinel.test").info("once")
    assert len(stream.getvalue().strip().splitlines()) == 1


def test_the_level_gate_is_honoured():
    stream = StringIO()
    configure_logging("ERROR", stream=stream)
    log = get_logger("sentinel.test")
    log.info("ignored")
    log.error("kept")
    events = [json.loads(line)["event"] for line in stream.getvalue().strip().splitlines()]
    assert events == ["kept"]


@pytest.mark.parametrize(
    ("card", "masked"),
    [("C0001", "C0***"), ("", ""), ("A", "*"), ("AB", "**"), ("4111111111111111", "41**************")],
)
def test_card_identifiers_are_masked_before_they_reach_a_log_line(card, masked):
    assert mask_card(card) == masked


def test_an_exception_is_recorded_by_type_not_by_payload():
    stream = StringIO()
    configure_logging("ERROR", stream=stream)
    log = get_logger("sentinel.test")
    try:
        raise ValueError("card 4111111111111111 was declined")
    except ValueError:
        log.error("append_failed", exc_info=True)
    payload = json.loads(stream.getvalue().strip())
    assert payload["exc_type"] == "ValueError"
    assert "4111111111111111" not in json.dumps(payload)


def test_the_formatter_survives_a_value_json_cannot_encode():
    record = logging.LogRecord("sentinel.test", logging.INFO, "f", 1, "odd", (), None)
    record.thing = object()
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "odd"
    assert isinstance(payload["thing"], str)


def test_the_timer_reports_milliseconds():
    timer = Timer()
    assert timer.ms >= 0.0
    assert isinstance(timer.ms, float)
