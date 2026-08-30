"""Live read model == replayed read model. The claim the architecture exists for.

Every other guarantee here rests on this one. If a projection carries state that did
not come off the log, then the queue an analyst sees is not the queue the log implies,
time travel is fiction, and the audit trail is a story rather than evidence.

The property was true before these tests existed — the bus applies the records the
*store* returned, not the handler's output, which is what makes it true — but nothing
asserted it. Several docstrings claimed it was asserted. Now it is.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from sentinel.ingest import build_event_log
from sentinel.projections.registry import ReadModel, default_projections
from sentinel.store.sqlite_store import EventStore

ROOT = Path(__file__).resolve().parents[1]


def test_live_and_replayed_read_models_are_byte_identical(store, live_read_model):
    rebuilt = ReadModel.rebuild(store)
    assert rebuilt.fingerprint() == live_read_model.fingerprint()


def test_the_comparison_is_per_projection_so_a_mismatch_names_the_culprit(store, live_read_model):
    """A single whole-model hash tells you something broke, not what."""
    rebuilt = ReadModel.rebuild(store)
    live = live_read_model.fingerprints()
    assert set(rebuilt.fingerprints()) == set(live)
    for name, digest in rebuilt.fingerprints().items():
        assert digest == live[name], f"{name} diverged between live and replay"


def test_rebuilding_twice_gives_the_same_answer(store):
    first, second = ReadModel.rebuild(store), ReadModel.rebuild(store)
    assert first.fingerprint() == second.fingerprint()
    assert first.snapshot() == second.snapshot()


def test_folding_in_chunks_equals_folding_in_one_pass(store):
    """Nothing may depend on how many records arrived per call."""
    whole = ReadModel.rebuild(store)
    chunked = ReadModel()
    cursor = 1
    while cursor <= store.last_seq:
        chunked.apply_all(store.read(from_seq=cursor, upto_seq=min(cursor + 999, store.last_seq)))
        cursor += 1000
    assert chunked.applied == whole.applied
    assert chunked.fingerprint() == whole.fingerprint()


def test_a_prefix_rebuild_never_sees_a_later_event(store):
    """The time-travel primitive: upto_seq is an exact cut, not an approximation."""
    cut = store.last_seq // 2
    past = ReadModel.rebuild(store, upto_seq=cut)
    assert past.upto_seq == cut
    assert past.applied == cut

    later_cases = {
        rec.event.case_id
        for rec in store.read(from_seq=cut + 1, types=["CaseOpened"])
    }
    known_then = {case.case_id for case in past.cases.all_cases()}
    assert not (later_cases & known_then)


def test_a_prefix_rebuild_matches_folding_that_prefix_by_hand(store):
    """Two independent routes to the same state."""
    cut = store.last_seq // 3
    direct = ReadModel.rebuild(store, upto_seq=cut)
    by_hand = ReadModel().apply_all(store.read(upto_seq=cut))
    assert direct.fingerprint() == by_hand.fingerprint()


def test_the_projection_set_is_what_the_read_model_exposes():
    """A projection added to the bundle must be reachable, or it folds in silence."""
    names = [p.name for p in default_projections()]
    assert names == sorted(names), "order must be stable, the fingerprint depends on it"
    model = ReadModel()
    assert model.names() == names
    for name in names:
        assert model[name].name == name


def test_two_builds_at_the_same_seed_produce_the_same_log(tmp_path, corpus):
    """Determinism end to end: same corpus, same head hash, same fingerprint."""
    heads = []
    prints = []
    for run in range(2):
        with EventStore.create_fresh(tmp_path / f"run{run}.db") as store:
            built = build_event_log(corpus, store, seed=11)
            heads.append(store.head_hash)
            prints.append(built.read_model.fingerprint())
    assert heads[0] == heads[1]
    assert prints[0] == prints[1]


REPLAY_WITHOUT_SKLEARN = textwrap.dedent(
    """
    import sys

    class Blocked:
        def find_module(self, name, path=None):
            if name == "sklearn" or name.startswith("sklearn."):
                raise ImportError("sklearn is not available on this machine")
            return None

        def find_spec(self, name, path=None, target=None):
            self.find_module(name)
            return None

    sys.meta_path.insert(0, Blocked())
    sys.path.insert(0, __ROOT__)

    from sentinel.projections.registry import ReadModel
    from sentinel.store.sqlite_store import EventStore

    store = EventStore(__PATH__)
    print(ReadModel.rebuild(store).fingerprint())
    assert "sklearn" not in sys.modules
    """
)


def test_the_read_side_replays_on_a_machine_with_no_sklearn(log_copy, live_read_model):
    """The point of recording scores rather than recomputing them.

    A child process with ``sklearn`` made unimportable must still reproduce the
    fingerprint exactly. If it cannot, some projection is re-deriving a decision
    instead of reading the one the log recorded.
    """
    log_copy.close()
    script = (
        REPLAY_WITHOUT_SKLEARN.replace("__ROOT__", repr(str(ROOT)))
        .replace("__PATH__", repr(str(log_copy.path)))
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == live_read_model.fingerprint()


def test_assess_and_assess_batch_agree_exactly(built, corpus):
    """``RiskEngine.assess`` claims to be the single-transaction sibling of the batch path."""
    from sentinel.detection.risk_engine import RiskEngine

    models = built.bus.handler_for(_observe_type()).risk.models
    sample = corpus.sort_values("ts").head(400).to_dict("records")
    txns = [
        {k: row[k] for k in ("txn_id", "card_id", "ts", "amount", "category", "lat", "lon",
                             "hour", "is_online")}
        for row in sample
    ]
    single_engine = RiskEngine(models)
    singles = [single_engine.assess(t) for t in txns]
    batched = RiskEngine(models).assess_batch(txns)
    assert singles == batched


def _observe_type():
    from sentinel.commands.messages import ObserveTransaction

    return ObserveTransaction


@pytest.mark.parametrize("chunk", [1, 7, 1000])
def test_apply_order_is_the_only_thing_that_matters(store, chunk):
    """Different batch sizes, same fold — no per-call state anywhere."""
    model = ReadModel()
    cursor = 1
    while cursor <= min(store.last_seq, 3000):
        model.apply_all(
            store.read(from_seq=cursor, upto_seq=min(cursor + chunk - 1, 3000))
        )
        cursor += chunk
    reference = ReadModel.rebuild(store, upto_seq=3000)
    assert model.fingerprint() == reference.fingerprint()
