"""Tests for T2 (#40): no-LLM semantic-retrieval candidate recall over the corpus.

These exercise the pure recall/dedupe/render logic in ``rekol.corpus_propose``
plus the ``propose --from-corpus`` CLI wiring. Hermetic: a throwaway
``SessionStore`` and ``IndexStore`` are built in tmp dirs with the deterministic
``HashingEmbedder`` (no ML model, no network), and the conftest autouse fixture
isolates the index cache / archive env.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from rekol.corpus_propose import (
    INSTRUCTION_SIGNAL_QUERIES,
    CorpusCandidate,
    dedupe_against_memory,
    recall_corpus_candidates,
    render_corpus_proposal,
)
from rekol.embeddings import HashingEmbedder
from rekol.sessions.store import SessionStore
from rekol.store import IndexStore


def _seed_session_store(db_path: Path, embedder: HashingEmbedder) -> SessionStore:
    """Build a SessionStore with a few instruction-signal-bearing messages."""
    store = SessionStore(db_path=db_path, dim=embedder.dim)
    store.init_schema()
    messages = [
        # A clear durable convention — should be recalled.
        dict(
            session_id="sess-1",
            message_uuid="uuid-1",
            parent_uuid=None,
            role="user",
            content="We always deploy via docker compose pull on the Dell box, never manually.",
            cwd="/home/leon/infra",
            timestamp_iso="2026-01-01T10:00:00Z",
            timestamp_unix=1767261600,
            jsonl_path="/transcripts/sess-1.jsonl",
            line_number=12,
        ),
        # A stated preference — should be recalled.
        dict(
            session_id="sess-1",
            message_uuid="uuid-2",
            parent_uuid="uuid-1",
            role="user",
            content="I prefer verbose descriptive variable names over short ones.",
            cwd="/home/leon/infra",
            timestamp_iso="2026-01-01T10:05:00Z",
            timestamp_unix=1767261900,
            jsonl_path="/transcripts/sess-1.jsonl",
            line_number=20,
        ),
        # A repo-location fact — should be recalled.
        dict(
            session_id="sess-2",
            message_uuid="uuid-3",
            parent_uuid=None,
            role="user",
            content="The rekol repo lives in ~/github/rekol on this machine.",
            cwd="/home/leon/github/rekol",
            timestamp_iso="2026-02-01T09:00:00Z",
            timestamp_unix=1769936400,
            jsonl_path="/transcripts/sess-2.jsonl",
            line_number=3,
        ),
        # Ordinary chatter that carries no instruction signal.
        dict(
            session_id="sess-2",
            message_uuid="uuid-4",
            parent_uuid="uuid-3",
            role="assistant",
            content="Sure, here is the function you asked for.",
            cwd="/home/leon/github/rekol",
            timestamp_iso="2026-02-01T09:01:00Z",
            timestamp_unix=1769936460,
            jsonl_path="/transcripts/sess-2.jsonl",
            line_number=4,
        ),
    ]
    for msg in messages:
        rowid = store.insert_message(msg)
        assert rowid is not None
        store.upsert_embedding(rowid, embedder.embed(msg["content"]))
    return store


def test_instruction_signal_queries_are_nonempty() -> None:
    """The driver queries must exist and cover the canonical signal phrasings."""
    joined = " ".join(INSTRUCTION_SIGNAL_QUERIES).lower()
    assert INSTRUCTION_SIGNAL_QUERIES
    for phrase in ("always", "prefer", "convention", "decided", "don't", "repo"):
        assert phrase in joined, f"expected signal phrasing {phrase!r} in driver queries"


def test_recall_returns_candidates_with_provenance(tmp_path: Path) -> None:
    """Recall surfaces corpus messages carrying full provenance (session + file + line)."""
    embedder = HashingEmbedder(dim=384)
    store = _seed_session_store(tmp_path / "sessions.db", embedder)
    try:
        candidates = recall_corpus_candidates(store, embedder, per_query_top_k=5)
    finally:
        store.close()

    assert candidates, "expected at least one recalled candidate"
    # Every candidate must carry provenance the operator can act on.
    for cand in candidates:
        assert isinstance(cand, CorpusCandidate)
        assert cand.session_id
        assert cand.jsonl_path
        assert cand.line_number >= 1
        assert cand.content
        assert cand.matched_query in INSTRUCTION_SIGNAL_QUERIES

    contents = " || ".join(c.content for c in candidates)
    # The instruction-bearing message should be recalled by the FTS keyword path
    # even though HashingEmbedder is not semantically meaningful.
    assert "docker compose pull" in contents


def test_recall_dedupes_same_message_across_queries(tmp_path: Path) -> None:
    """A message matched by several driver queries appears once, at its best score."""
    embedder = HashingEmbedder(dim=384)
    store = _seed_session_store(tmp_path / "sessions.db", embedder)
    try:
        candidates = recall_corpus_candidates(store, embedder, per_query_top_k=5)
    finally:
        store.close()

    keys = [(c.session_id, c.message_uuid) for c in candidates]
    assert len(keys) == len(set(keys)), f"duplicate (session, uuid) in candidates: {keys}"


def test_recall_respects_max_candidates_cap(tmp_path: Path) -> None:
    """The corpus-narrowing cap bounds the candidate list (cheap, recall-oriented)."""
    embedder = HashingEmbedder(dim=384)
    store = _seed_session_store(tmp_path / "sessions.db", embedder)
    try:
        candidates = recall_corpus_candidates(store, embedder, per_query_top_k=5, max_candidates=2)
    finally:
        store.close()
    assert len(candidates) <= 2


def test_dedupe_against_memory_splits_new_from_captured(tmp_path: Path) -> None:
    """Candidates already in curated memory are separated from genuinely-new ones."""
    embedder = HashingEmbedder(dim=384)
    memory_home = tmp_path / "home"
    (memory_home / "topics").mkdir(parents=True)
    # Seed a curated memory chunk that matches one candidate verbatim, so the
    # cosine dedupe is forced above threshold for the HashingEmbedder.
    captured_text = "We always deploy via docker compose pull on the Dell box, never manually."
    memory_store = IndexStore(
        db_path=tmp_path / "index.db", dim=embedder.dim, embedding_model="test-hashing"
    )
    memory_store.init_schema()
    memory_store.replace_file_and_chunks(
        path=str(memory_home / "topics" / "deploy.md"),
        mtime=0,
        content_hash="h",
        name="Deploy",
        chunks=[
            dict(
                heading="Deploy",
                line_start=1,
                line_end=1,
                text=captured_text,
                embedding=embedder.embed(captured_text),
                tags=[],
                aliases=[],
            )
        ],
    )

    candidates = [
        CorpusCandidate(
            content=captured_text,
            session_id="sess-1",
            message_uuid="uuid-1",
            role="user",
            cwd="/x",
            timestamp_iso="2026-01-01T10:00:00Z",
            jsonl_path="/transcripts/sess-1.jsonl",
            line_number=12,
            score=0.9,
            matched_query=INSTRUCTION_SIGNAL_QUERIES[0],
        ),
        CorpusCandidate(
            content="The frobnicator widget should glow purple on Tuesdays only.",
            session_id="sess-9",
            message_uuid="uuid-9",
            role="user",
            cwd="/y",
            timestamp_iso="2026-03-01T10:00:00Z",
            jsonl_path="/transcripts/sess-9.jsonl",
            line_number=5,
            score=0.7,
            matched_query=INSTRUCTION_SIGNAL_QUERIES[0],
        ),
    ]
    try:
        new_candidates, already_captured = dedupe_against_memory(candidates, memory_store, embedder)
    finally:
        memory_store.close()

    captured_contents = {c.content for c, _hit in already_captured}
    new_contents = {c.content for c in new_candidates}
    assert captured_text in captured_contents
    assert "frobnicator widget" in " ".join(new_contents)
    assert captured_text not in new_contents


def test_render_proposal_includes_source_refs_and_no_autowrite_note() -> None:
    """The rendered proposal carries provenance per candidate and a review banner."""
    cand = CorpusCandidate(
        content="We always deploy via docker compose pull.",
        session_id="sess-1",
        message_uuid="uuid-1",
        role="user",
        cwd="/home/leon/infra",
        timestamp_iso="2026-01-01T10:00:00Z",
        jsonl_path="/transcripts/sess-1.jsonl",
        line_number=12,
        score=0.91,
        matched_query=INSTRUCTION_SIGNAL_QUERIES[0],
    )
    body = render_corpus_proposal(
        new_candidates=[cand], already_captured=[], timestamp="20260101-100000"
    )
    assert "We always deploy via docker compose pull." in body
    # Provenance: session id + file + line so the operator can verify the source.
    assert "sess-1" in body
    assert "/transcripts/sess-1.jsonl" in body
    assert "12" in body
    # Honesty: nothing is auto-written; this is a human-review checklist.
    assert "review" in body.lower()


# --------------------------- CLI integration ---------------------------


def _seed_memory_home(root: Path) -> None:
    """Minimal memory home with the test-hashing embedder configured."""
    (root / "topics").mkdir(parents=True, exist_ok=True)
    (root / "rekol.config.yaml").write_text("embedding_model: test-hashing\n")


def test_propose_from_corpus_writes_pending_review(tmp_path: Path, monkeypatch) -> None:
    """`propose --from-corpus` recalls from sessions.db and writes a review file."""
    from rekol.cli_propose import main as propose_main
    from rekol.config import load_config

    memory_home = tmp_path / "home"
    _seed_memory_home(memory_home)
    monkeypatch.setenv("REKOL_HOME", str(memory_home))

    # Build the sessions index at the resolved (isolated) cache path so the CLI
    # finds it where load_config() expects it.
    embedder = HashingEmbedder(dim=384)
    cfg = load_config()
    store = _seed_session_store(cfg.sessions_db_path, embedder)
    store.close()

    runner = CliRunner()
    result = runner.invoke(propose_main, ["--from-corpus"])
    assert result.exit_code == 0, result.output

    pending = list((memory_home / "pending-review").glob("*.md"))
    assert len(pending) == 1, list((memory_home / "pending-review").iterdir())
    body = pending[0].read_text()
    assert "docker compose pull" in body
    # Provenance present so the candidate is traceable to its transcript.
    assert "sess-1" in body


def test_propose_from_corpus_empty_index_is_graceful(tmp_path: Path, monkeypatch) -> None:
    """With no sessions indexed, the command reports no candidates and exits 0."""
    from rekol.cli_propose import main as propose_main

    memory_home = tmp_path / "home"
    _seed_memory_home(memory_home)
    monkeypatch.setenv("REKOL_HOME", str(memory_home))

    runner = CliRunner()
    result = runner.invoke(propose_main, ["--from-corpus"])
    assert result.exit_code == 0, result.output
    assert "no candidates" in result.output.lower()
