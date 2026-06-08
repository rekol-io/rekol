"""Tests for T3 (#41) bootstrap checkpoint/state module.

Resumability is the hard AC: a long bootstrap run over a big corpus WILL get
interrupted (reaped, killed, ^C). These exercise the small JSON state file that
records the batch plan + which batches finished, so a re-run skips completed
work instead of reprocessing it. Hermetic: state lives in a tmp dir; no network,
no ML model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rekol.bootstrap_state import (
    BootstrapState,
    load_state,
    save_state,
    state_path_for,
)


def _fresh_state() -> BootstrapState:
    return BootstrapState(
        run_id="20260607-120000",
        embedding_model="test-hashing",
        scope={"projects": ["rekol"], "days": 30, "max_sessions": 100},
        batches=["batch-a", "batch-b", "batch-c"],
        completed=[],
        created_iso="2026-06-07T12:00:00-04:00",
        updated_iso="2026-06-07T12:00:00-04:00",
        candidates_written=0,
    )


def test_state_path_lives_under_pending_review(tmp_path: Path) -> None:
    """The checkpoint file is colocated with the candidate artifacts it tracks."""
    p = state_path_for(tmp_path)
    assert p.parent == tmp_path / "pending-review"
    assert p.name.startswith(".bootstrap-state")
    assert p.suffix == ".json"


def test_save_then_load_roundtrips(tmp_path: Path) -> None:
    """A saved state reloads identically — the resume contract depends on this."""
    state = _fresh_state()
    path = state_path_for(tmp_path)
    save_state(state, path)
    loaded = load_state(path)
    assert loaded == state


def test_save_is_atomic_no_partial_temp_left(tmp_path: Path) -> None:
    """Atomic write (tmp + rename) leaves no dangling temp file behind."""
    state = _fresh_state()
    path = state_path_for(tmp_path)
    save_state(state, path)
    leftovers = [p for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == [], f"unexpected leftover files: {leftovers}"
    # The file is valid JSON (not a half-written stream).
    json.loads(path.read_text())


def test_load_missing_returns_none(tmp_path: Path) -> None:
    """No state file means no prior run — load returns None, never raises."""
    assert load_state(state_path_for(tmp_path)) is None


def test_mark_batch_done_is_idempotent() -> None:
    """Marking the same batch twice doesn't duplicate it (a retried run is safe)."""
    state = _fresh_state()
    state.mark_batch_done("batch-a")
    state.mark_batch_done("batch-a")
    assert state.completed == ["batch-a"]


def test_pending_batches_excludes_completed_preserving_plan_order() -> None:
    """Resume processes only not-yet-done batches, in the original plan order."""
    state = _fresh_state()
    state.mark_batch_done("batch-b")
    assert state.pending_batches() == ["batch-a", "batch-c"]


def test_is_complete_true_only_when_all_batches_done() -> None:
    """A run is complete iff every planned batch has finished."""
    state = _fresh_state()
    assert not state.is_complete()
    for b in state.batches:
        state.mark_batch_done(b)
    assert state.is_complete()


def test_mark_unknown_batch_done_is_rejected() -> None:
    """Marking a batch that isn't in the plan is a programming error, not silent."""
    state = _fresh_state()
    with pytest.raises(ValueError, match="not in the batch plan"):
        state.mark_batch_done("batch-zzz")


def test_load_corrupt_state_raises_clear_error(tmp_path: Path) -> None:
    """A truncated/corrupt checkpoint must fail loudly, not silently lose progress."""
    path = state_path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    with pytest.raises(ValueError, match="corrupt"):
        load_state(path)


def test_scope_matches_guards_resume_against_changed_scope() -> None:
    """A resume with different scope params must be detectable (don't mix runs)."""
    state = _fresh_state()
    assert state.scope_matches({"projects": ["rekol"], "days": 30, "max_sessions": 100})
    assert not state.scope_matches({"projects": ["other"], "days": 30, "max_sessions": 100})
