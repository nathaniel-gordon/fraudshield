"""Checkpoints must be an optimisation and nothing else.

Every test here exists to pin the same property from a different side: a read model
resumed from a checkpoint is indistinguishable from one folded from sequence 1. The
moment that stops being true, a checkpoint is no longer a cache — it is a second,
disagreeing source of truth, and the audit trail is worthless.

So the assertions are deliberately on *fingerprints and full snapshots*, not on
spot-checked fields, and the failure cases (wrong log, corrupted state, stale code)
all assert the fallback actually happened rather than that no exception was raised.
"""
from __future__ import annotations

import json

import pytest

from sentinel.errors import CheckpointUnusable
from sentinel.projections.checkpoints import (
    Checkpoint,
    CheckpointStore,
    rebuild_with_checkpoints,
)
from sentinel.projections.registry import ReadModel

# --------------------------------------------------------------- round tripping


def test_every_projection_round_trips_through_checkpoint(store):
    """restore(checkpoint()) must reproduce a projection exactly, not approximately."""
    full = ReadModel.rebuild(store)
    for name in full.names():
        revived = ReadModel()[name].__class__()
        revived.restore(full[name].checkpoint())
        assert revived.snapshot() == full[name].snapshot(), name
        assert revived.fingerprint() == full[name].fingerprint(), name


def test_checkpoint_state_is_json_serialisable(store):
    """The state goes into a TEXT column, and it must never need pickle to do so."""
    model = ReadModel.rebuild(store)
    for name in model.names():
        encoded = json.dumps(model[name].checkpoint(), sort_keys=True)
        assert json.loads(encoded) is not None, name


def test_resume_midway_equals_a_full_fold(store):
    """The load-bearing property: fold-to-N, restore, fold-the-rest == fold-from-1."""
    half = store.last_seq // 2
    partial = ReadModel().apply_all(store.read(upto_seq=half))

    resumed = ReadModel()
    for name in resumed.names():
        resumed[name].restore(partial[name].checkpoint())
    resumed.apply_all(store.read(from_seq=half + 1))

    reference = ReadModel.rebuild(store)
    assert resumed.fingerprints() == reference.fingerprints()
    assert resumed.snapshot() == reference.snapshot()


# ------------------------------------------------------------------ persistence


def test_saved_checkpoint_rebuild_matches_from_zero(log_copy):
    """The whole point, through the real table: same fingerprint, fewer records read."""
    reference = ReadModel.rebuild(log_copy)

    midpoint = log_copy.last_seq // 2
    CheckpointStore(log_copy).save(ReadModel().apply_all(log_copy.read(upto_seq=midpoint)))

    resumed = rebuild_with_checkpoints(log_copy)
    assert resumed.resumed_from == midpoint
    assert resumed.fingerprint() == reference.fingerprint()
    assert resumed.fingerprints() == reference.fingerprints()
    assert resumed.snapshot() == reference.snapshot()
    # applied is restored from the checkpoint, so the resumed model still reports the
    # true number of records behind its state rather than only the ones it re-read.
    assert resumed.applied == reference.applied
    assert resumed.upto_seq == reference.upto_seq


def test_checkpointed_time_travel_matches_a_plain_prefix_fold(log_copy):
    """Time travel is where checkpoints earn their place; it must not change answers."""
    midpoint = log_copy.last_seq // 2
    CheckpointStore(log_copy).save(ReadModel().apply_all(log_copy.read(upto_seq=midpoint)))

    target = midpoint + (log_copy.last_seq - midpoint) // 2
    assert rebuild_with_checkpoints(log_copy, target).fingerprint() == (
        ReadModel.rebuild(log_copy, target).fingerprint()
    )


def test_a_checkpoint_after_the_target_is_not_used(log_copy):
    """Resuming from the future would show events the caller asked not to see."""
    late = log_copy.last_seq
    CheckpointStore(log_copy).save(ReadModel.rebuild(log_copy))

    early = late // 4
    resumed = rebuild_with_checkpoints(log_copy, early)
    assert resumed.resumed_from == 0
    assert resumed.fingerprint() == ReadModel.rebuild(log_copy, early).fingerprint()


def test_no_checkpoints_means_a_plain_rebuild(log_copy):
    assert CheckpointStore(log_copy).count() == 0
    model = rebuild_with_checkpoints(log_copy)
    assert model.resumed_from == 0
    assert model.fingerprint() == ReadModel.rebuild(log_copy).fingerprint()


def test_newest_usable_checkpoint_wins(log_copy):
    checkpoints = CheckpointStore(log_copy)
    first = log_copy.last_seq // 4
    second = log_copy.last_seq // 2
    for seq in (first, second):
        checkpoints.save(ReadModel().apply_all(log_copy.read(upto_seq=seq)))
    assert rebuild_with_checkpoints(log_copy).resumed_from == second


# --------------------------------------------------------------- refusing to lie


def test_checkpoint_from_a_different_log_is_ignored(log_copy):
    """A chain hash that does not match means this state describes some other log."""
    checkpoints = CheckpointStore(log_copy)
    midpoint = log_copy.last_seq // 2
    saved = checkpoints.save(ReadModel().apply_all(log_copy.read(upto_seq=midpoint)))

    log_copy.connection.execute(
        "UPDATE projection_checkpoints SET chain_hash = ? WHERE seq = ?",
        ("0" * 64, saved.seq),
    )
    log_copy.connection.commit()

    model = rebuild_with_checkpoints(log_copy)
    assert model.resumed_from == 0
    assert model.fingerprint() == ReadModel.rebuild(log_copy).fingerprint()


def test_tampered_state_is_caught_by_the_fingerprint(log_copy):
    """Editing the cached state must not change what a rebuild reports."""
    checkpoints = CheckpointStore(log_copy)
    midpoint = log_copy.last_seq // 2
    saved = checkpoints.save(ReadModel().apply_all(log_copy.read(upto_seq=midpoint)))

    row = log_copy.connection.execute(
        "SELECT state FROM projection_checkpoints WHERE seq = ?", (saved.seq,)
    ).fetchone()
    state = json.loads(row[0])
    doctored = dict(state)
    doctored["daily_metrics"] = dict(doctored["daily_metrics"], labelled_fraud=999999)
    log_copy.connection.execute(
        "UPDATE projection_checkpoints SET state = ? WHERE seq = ?",
        (json.dumps(doctored), saved.seq),
    )
    log_copy.connection.commit()

    model = rebuild_with_checkpoints(log_copy)
    assert model.resumed_from == 0
    assert model.fingerprint() == ReadModel.rebuild(log_copy).fingerprint()


def test_unparseable_state_falls_back_instead_of_raising(log_copy):
    saved = CheckpointStore(log_copy).save(ReadModel.rebuild(log_copy))
    log_copy.connection.execute(
        "UPDATE projection_checkpoints SET state = ? WHERE seq = ?", ("{not json", saved.seq)
    )
    log_copy.connection.commit()

    model = rebuild_with_checkpoints(log_copy)
    assert model.resumed_from == 0
    assert model.fingerprint() == ReadModel.rebuild(log_copy).fingerprint()


def test_checkpoint_naming_a_projection_we_do_not_have_is_ignored(log_copy):
    saved = CheckpointStore(log_copy).save(ReadModel.rebuild(log_copy))
    log_copy.connection.execute(
        "UPDATE projection_checkpoints SET names = ? WHERE seq = ?",
        (json.dumps(["case_queue", "something_else"]), saved.seq),
    )
    log_copy.connection.commit()
    assert rebuild_with_checkpoints(log_copy).resumed_from == 0


def test_saving_an_empty_read_model_is_refused(log_copy):
    with pytest.raises(CheckpointUnusable):
        CheckpointStore(log_copy).save(ReadModel())


def test_saving_at_a_sequence_the_log_does_not_have_is_refused(log_copy):
    model = ReadModel.rebuild(log_copy)
    model.upto_seq = log_copy.last_seq + 5000
    with pytest.raises(CheckpointUnusable):
        CheckpointStore(log_copy).save(model)


# ---------------------------------------------------------------- housekeeping


def test_saving_twice_at_one_sequence_replaces_rather_than_duplicates(log_copy):
    checkpoints = CheckpointStore(log_copy)
    model = ReadModel.rebuild(log_copy)
    checkpoints.save(model)
    checkpoints.save(model)
    assert checkpoints.count() == 1


def test_prune_keeps_the_newest(log_copy):
    checkpoints = CheckpointStore(log_copy)
    step = max(1, log_copy.last_seq // 5)
    seqs = [step * n for n in range(1, 5)]
    for seq in seqs:
        checkpoints.save(ReadModel().apply_all(log_copy.read(upto_seq=seq)))
    assert checkpoints.count() == len(seqs)

    removed = checkpoints.prune(keep=2)
    assert removed == len(seqs) - 2
    assert [c.seq for c in checkpoints.list()] == sorted(seqs[-2:], reverse=True)


def test_prune_to_nothing_leaves_rebuilds_working(log_copy):
    checkpoints = CheckpointStore(log_copy)
    checkpoints.save(ReadModel.rebuild(log_copy))
    checkpoints.prune(keep=0)
    assert checkpoints.count() == 0
    assert rebuild_with_checkpoints(log_copy).fingerprint() == (
        ReadModel.rebuild(log_copy).fingerprint()
    )


def test_prune_rejects_a_negative_keep(log_copy):
    with pytest.raises(ValueError):
        CheckpointStore(log_copy).prune(keep=-1)


def test_list_is_paged(log_copy):
    checkpoints = CheckpointStore(log_copy)
    step = max(1, log_copy.last_seq // 4)
    for seq in (step, step * 2, step * 3):
        checkpoints.save(ReadModel().apply_all(log_copy.read(upto_seq=seq)))
    assert len(checkpoints.list(limit=2)) == 2
    assert len(checkpoints.list(limit=2, offset=2)) == 1
    assert checkpoints.list(limit=1)[0].seq == checkpoints.latest_at_or_before(10**9).seq


def test_checkpoint_row_is_reportable(log_copy):
    saved = CheckpointStore(log_copy).save(ReadModel.rebuild(log_copy))
    payload = saved.as_dict()
    assert payload["seq"] == log_copy.last_seq
    assert payload["names"] == sorted(ReadModel().names())
    assert len(payload["chain_hash"]) == 64
    assert isinstance(saved, Checkpoint)


def test_latest_at_or_before_respects_the_bound(log_copy):
    checkpoints = CheckpointStore(log_copy)
    midpoint = log_copy.last_seq // 2
    checkpoints.save(ReadModel().apply_all(log_copy.read(upto_seq=midpoint)))
    assert checkpoints.latest_at_or_before(midpoint).seq == midpoint
    assert checkpoints.latest_at_or_before(midpoint - 1) is None
