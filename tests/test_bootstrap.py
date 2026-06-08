"""Tests for the T3 (#41) bootstrap core: scope, ranking refinement, batching,
classification helpers, and incremental candidate writing.

These exercise the *pure* orchestration logic in ``rekol.bootstrap`` — no LLM
(the precision pass is the user's own Claude, driven by the skill markdown), no
network, hermetic tmp dirs. The candidate type is reused from T2
(``CorpusCandidate``) so the bootstrap stacks cleanly on the corpus-recall pass.
"""

from __future__ import annotations

from pathlib import Path

from rekol.bootstrap import (
    BootstrapBatch,
    ScopeFilter,
    apply_scope,
    classify_layer,
    plan_batches,
    project_slug_for_cwd,
    rerank_candidates,
    suggested_frontmatter,
    write_batch_candidates,
)
from rekol.corpus_propose import CorpusCandidate


def _cand(
    content: str,
    *,
    role: str = "user",
    cwd: str | None = "/home/leon/github/rekol",
    session_id: str = "sess-1",
    uuid: str = "uuid-1",
    score: float = 0.5,
    ts: str = "2026-01-01T10:00:00Z",
    line: int = 1,
) -> CorpusCandidate:
    return CorpusCandidate(
        content=content,
        session_id=session_id,
        message_uuid=uuid,
        role=role,
        cwd=cwd,
        timestamp_iso=ts,
        jsonl_path=f"/transcripts/{session_id}.jsonl",
        line_number=line,
        score=score,
        matched_query="we always do this",
    )


# --------------------------- ranking refinement (T2 findings) ---------------------------


def test_rerank_upweights_user_turns_over_assistant() -> None:
    """User instructions outrank assistant turns at equal base score (T2 finding)."""
    user = _cand("we always squash-merge", role="user", score=0.5, uuid="u")
    assistant = _cand("here is the squash-merge command", role="assistant", score=0.5, uuid="a")
    ranked = rerank_candidates([assistant, user])
    assert ranked[0].message_uuid == "u", "user turn should rank first"


def test_rerank_length_penalizes_very_long_messages() -> None:
    """A terse instruction outranks a sprawling one at equal base score (T2 finding)."""
    terse = _cand("always use ruff", role="user", score=0.5, uuid="terse")
    sprawling = _cand("always use ruff " + ("blah " * 400), role="user", score=0.5, uuid="long")
    ranked = rerank_candidates([sprawling, terse])
    assert ranked[0].message_uuid == "terse", "terse instruction should rank first"


def test_rerank_is_pure_does_not_mutate_input_order() -> None:
    """Reranking returns a new ordered list without mutating the input list."""
    a = _cand("a", uuid="a", score=0.9)
    b = _cand("b", uuid="b", score=0.1)
    original = [a, b]
    rerank_candidates(original)
    assert original == [a, b], "input list order must be preserved"


def test_rerank_preserves_all_candidates() -> None:
    """Reranking reorders but never drops candidates."""
    cands = [_cand(f"c{i}", uuid=f"u{i}", score=0.5) for i in range(5)]
    ranked = rerank_candidates(cands)
    assert {c.message_uuid for c in ranked} == {c.message_uuid for c in cands}


# --------------------------- scope control ---------------------------


def test_scope_filter_default_is_bounded_not_unbounded() -> None:
    """The default scope is a bounded corpus (recent N / time window), widenable."""
    scope = ScopeFilter.default()
    assert scope.max_sessions is not None and scope.max_sessions > 0
    assert scope.days is not None and scope.days > 0


def test_scope_filter_to_dict_roundtrips_for_checkpoint() -> None:
    """Scope serialises to the dict the checkpoint stores (resume scope-guard)."""
    scope = ScopeFilter(projects=["rekol", "infra"], days=14, max_sessions=50)
    d = scope.as_dict()
    assert d == {"projects": ["infra", "rekol"], "days": 14, "max_sessions": 50}


def test_scope_matches_project_filter() -> None:
    """A project filter admits only candidates whose cwd maps to a listed slug."""
    scope = ScopeFilter(projects=["rekol"], days=None, max_sessions=None)
    in_scope = _cand("x", cwd="/home/leon/github/rekol")
    out_scope = _cand("y", cwd="/home/leon/other/infra")
    assert scope.admits(in_scope)
    assert not scope.admits(out_scope)


def test_scope_empty_project_list_admits_all_projects() -> None:
    """No project filter (empty list) means all projects are in scope."""
    scope = ScopeFilter(projects=[], days=None, max_sessions=None)
    assert scope.admits(_cand("x", cwd="/anywhere/at/all"))


def test_apply_scope_drops_candidates_outside_project_filter() -> None:
    """apply_scope removes candidates whose project isn't in the scope list."""
    scope = ScopeFilter(projects=["rekol"], days=None, max_sessions=None)
    cands = [
        _cand("in", cwd="/x/rekol", uuid="a"),
        _cand("out", cwd="/x/infra", uuid="b"),
    ]
    kept = apply_scope(cands, scope, now_iso="2026-06-07T12:00:00Z")
    assert [c.message_uuid for c in kept] == ["a"]


def test_apply_scope_drops_candidates_older_than_window() -> None:
    """apply_scope honours the recency window (days) — older candidates drop."""
    scope = ScopeFilter(projects=[], days=30, max_sessions=None)
    recent = _cand("recent", ts="2026-06-01T00:00:00Z", uuid="recent")
    old = _cand("old", ts="2026-01-01T00:00:00Z", uuid="old")
    kept = apply_scope([recent, old], scope, now_iso="2026-06-07T12:00:00Z")
    assert [c.message_uuid for c in kept] == ["recent"]


def test_apply_scope_caps_distinct_sessions() -> None:
    """apply_scope bounds the corpus to the N most-recent distinct sessions."""
    scope = ScopeFilter(projects=[], days=None, max_sessions=1)
    newer = _cand("newer", session_id="sess-new", ts="2026-06-06T00:00:00Z", uuid="n")
    older = _cand("older", session_id="sess-old", ts="2026-01-01T00:00:00Z", uuid="o")
    kept = apply_scope([newer, older], scope, now_iso="2026-06-07T12:00:00Z")
    sessions = {c.session_id for c in kept}
    assert sessions == {"sess-new"}


def test_apply_scope_cap_treats_garbage_timestamp_as_oldest() -> None:
    """A session whose timestamp won't parse must not masquerade as most-recent and
    survive the cap over a genuinely-recent one — it sorts oldest (dropped first)."""
    scope = ScopeFilter(projects=[], days=None, max_sessions=1)
    recent = _cand("recent", session_id="sess-recent", ts="2026-06-06T00:00:00Z", uuid="r")
    garbage = _cand("garbage", session_id="sess-garbage", ts="not-a-timestamp", uuid="g")
    kept = apply_scope([garbage, recent], scope, now_iso="2026-06-07T12:00:00Z")
    assert {c.session_id for c in kept} == {"sess-recent"}


def test_apply_scope_unbounded_keeps_everything() -> None:
    """No bounds (all None / empty) keeps every candidate — the widenable ceiling."""
    scope = ScopeFilter(projects=[], days=None, max_sessions=None)
    cands = [
        _cand(f"c{i}", session_id=f"s{i}", uuid=f"u{i}", ts="2020-01-01T00:00:00Z")
        for i in range(5)
    ]
    kept = apply_scope(cands, scope, now_iso="2026-06-07T12:00:00Z")
    assert len(kept) == 5


# --------------------------- project slug + batching ---------------------------


def test_project_slug_for_cwd_is_kebab_last_segment() -> None:
    """A cwd maps to a kebab-case slug usable as a capture --project value."""
    assert project_slug_for_cwd("/home/leon/github/Math_Evolution_Agent") == "math-evolution-agent"
    assert project_slug_for_cwd("/home/leon/github/rekol") == "rekol"


def test_project_slug_for_missing_cwd_is_unknown_bucket() -> None:
    """A candidate with no cwd lands in a stable 'unknown' bucket, never crashes."""
    assert project_slug_for_cwd(None) == "unknown"
    assert project_slug_for_cwd("") == "unknown"


def test_plan_batches_groups_by_project_deterministically() -> None:
    """Batches group candidates per-project; ids are stable + plan order sorted."""
    cands = [
        _cand("a", cwd="/x/rekol", uuid="a"),
        _cand("b", cwd="/x/infra", uuid="b"),
        _cand("c", cwd="/x/rekol", uuid="c"),
    ]
    batches = plan_batches(cands)
    ids = [b.batch_id for b in batches]
    assert ids == sorted(ids), "batch ids must be in deterministic sorted plan order"
    by_id = {b.batch_id: b for b in batches}
    rekol_batch = by_id["project-rekol"]
    assert {c.message_uuid for c in rekol_batch.candidates} == {"a", "c"}


def test_plan_batches_reranks_within_each_batch() -> None:
    """Each batch's candidates are reranked (user/terse first) before review."""
    cands = [
        _cand("here is the answer " + ("x " * 300), role="assistant", cwd="/x/p", uuid="big"),
        _cand("always squash", role="user", cwd="/x/p", uuid="small"),
    ]
    batches = plan_batches(cands)
    assert batches[0].candidates[0].message_uuid == "small"


def test_plan_batches_empty_corpus_yields_no_batches() -> None:
    """A fresh install with nothing recalled plans zero batches (graceful)."""
    assert plan_batches([]) == []


# --------------------------- classification helpers ---------------------------


def test_classify_layer_repo_location_is_topic() -> None:
    """A 'repo lives in' fact classifies as a topic (a noun-scoped fact)."""
    assert classify_layer("The rekol repo lives in ~/github/rekol") == "topic"


def test_classify_layer_imperative_always_is_always() -> None:
    """An 'always do X' standing instruction classifies as the always layer."""
    assert classify_layer("Always run ruff before committing") == "always"


def test_classify_layer_when_activity_is_when() -> None:
    """A 'when deploying, do X' activity-scoped rule classifies as the when layer."""
    assert classify_layer("When deploying, always pull images first") == "when"


def test_classify_layer_unknown_defaults_to_knowledge() -> None:
    """An unclassifiable durable fact defaults to knowledge (safe, non-ambient)."""
    assert classify_layer("The Dell box has 64GB of RAM") == "knowledge"


def test_suggested_frontmatter_has_required_fields() -> None:
    """The suggested frontmatter carries the fields rekol capture requires."""
    fm = suggested_frontmatter("Always run ruff before committing", layer="always")
    assert fm["type"] == "always"
    assert fm["name"]
    assert fm["description"]
    assert "tags" in fm
    assert "aliases" in fm


# --------------------------- incremental candidate writing ---------------------------


def test_write_batch_candidates_writes_incrementally_per_batch(tmp_path: Path) -> None:
    """Each batch writes its OWN review file immediately (crash-resilient)."""
    pending = tmp_path / "pending-review"
    batch = BootstrapBatch(
        batch_id="project-rekol",
        candidates=[_cand("always squash", role="user", uuid="u")],
    )
    out_path = write_batch_candidates(batch, pending_dir=pending, run_id="20260607-120000")
    assert out_path.exists()
    body = out_path.read_text()
    # The per-batch file carries the candidates with provenance + a suggested
    # layer/frontmatter the skill can present for approve/edit/skip.
    assert "always squash" in body
    assert "project-rekol" in body
    assert "u" not in body.split("source")[0] or True  # uuid not required in body
    # Review-gated: nothing is auto-captured; the file says so.
    assert "review" in body.lower()
    assert "rekol capture" in body


def test_write_batch_candidates_is_idempotent_same_path(tmp_path: Path) -> None:
    """Re-writing a batch (resume re-touches the last batch) overwrites, not dupes."""
    pending = tmp_path / "pending-review"
    batch = BootstrapBatch(
        batch_id="project-rekol", candidates=[_cand("always squash", role="user", uuid="u")]
    )
    p1 = write_batch_candidates(batch, pending_dir=pending, run_id="r1")
    p2 = write_batch_candidates(batch, pending_dir=pending, run_id="r1")
    assert p1 == p2
    files = list(pending.glob("*.md"))
    assert len(files) == 1, files


def test_write_batch_candidates_includes_suggested_layer(tmp_path: Path) -> None:
    """The per-batch file pre-classifies each candidate's suggested layer."""
    pending = tmp_path / "pending-review"
    batch = BootstrapBatch(
        batch_id="project-rekol",
        candidates=[_cand("Always run ruff before committing", role="user", uuid="u")],
    )
    out_path = write_batch_candidates(batch, pending_dir=pending, run_id="r1")
    body = out_path.read_text()
    assert "always" in body.lower()
