"""Smoke tests for claude-session-index CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

from click.testing import CliRunner

from rekol.cli_session_index import main as cli_main
from rekol.embeddings import HashingEmbedder
from rekol.sessions.store import SessionStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


def _setup_home(tmp_path: Path, monkeypatch) -> Path:
    """Create a memory home with a fake projects dir holding the fixture.

    Uses the ``test-hashing`` embedder so the embedding path runs without
    loading the real sentence-transformers model.
    """
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "fake-projects" / "proj-a"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURE, projects / "session.jsonl")
    monkeypatch.setenv("MEMORY_HOME", str(home))
    (home / "memory.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\nembedding_model: test-hashing\n"
    )
    return home


def test_session_index_full_runs_against_directory(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "fake-projects" / "proj-a"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURE, projects / "session.jsonl")

    monkeypatch.setenv("MEMORY_HOME", str(home))
    (home / "memory.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\nembedding_model: test-hashing\n"
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--full"])
    assert result.exit_code == 0, result.output
    assert "messages_inserted=3" in result.output
    assert (home / ".index" / "sessions.db").exists()


def test_session_index_incremental_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "fake-projects" / "proj-a"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURE, projects / "session.jsonl")

    monkeypatch.setenv("MEMORY_HOME", str(home))
    (home / "memory.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\nembedding_model: test-hashing\n"
    )

    runner = CliRunner()
    setup_result = runner.invoke(cli_main, ["--full"])
    assert setup_result.exit_code == 0, setup_result.output
    result = runner.invoke(cli_main, ["--incremental"])
    assert result.exit_code == 0, result.output
    # Incremental with mtime-skip should NOT touch the file at all
    assert "messages_inserted=0" in result.output
    assert "files_skipped_unchanged=1" in result.output


def test_session_index_embeds_by_default(tmp_path: Path, monkeypatch) -> None:
    """Embedding is on by default, so a full ingest must populate the vector
    index — transcript search is semantic, not keyword-only.
    """
    home = _setup_home(tmp_path, monkeypatch)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--full"])
    assert result.exit_code == 0, result.output
    assert "messages_inserted=3" in result.output

    store = SessionStore(db_path=home / ".index" / "sessions.db", dim=384)
    store.init_schema()
    query_vec = HashingEmbedder(dim=384).embed("second user turn with list content")
    hits = store.search_vec(query_vec, top_k=3)
    store.close()
    assert hits, "default ingest left the vector index empty"
    assert hits[0]["message_uuid"] == "u-3"


def test_session_index_no_embed_leaves_vector_index_empty(tmp_path: Path, monkeypatch) -> None:
    """--no-embed is the explicit fast/keyword-only path: no vectors written."""
    home = _setup_home(tmp_path, monkeypatch)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--full", "--no-embed"])
    assert result.exit_code == 0, result.output
    assert "messages_inserted=3" in result.output

    store = SessionStore(db_path=home / ".index" / "sessions.db", dim=384)
    store.init_schema()
    query_vec = HashingEmbedder(dim=384).embed("second user turn with list content")
    hits = store.search_vec(query_vec, top_k=3)
    store.close()
    assert hits == []


def test_session_index_embed_heals_a_no_embed_index(tmp_path: Path, monkeypatch) -> None:
    """Regression for the mtime-skip gap: an index first built with --no-embed
    is skipped forever by the file gate, so a later default (embed) run must
    self-heal it via the repair pass — without it the dupe rows return no rowid
    and would never be embedded.
    """
    home = _setup_home(tmp_path, monkeypatch)
    runner = CliRunner()

    # Build FTS-only first.
    r1 = runner.invoke(cli_main, ["--full", "--no-embed"])
    assert r1.exit_code == 0, r1.output

    # Now a normal run (embed on). The files are unchanged so the walk skips
    # them; only the repair pass can make this index semantic.
    r2 = runner.invoke(cli_main, ["--incremental"])
    assert r2.exit_code == 0, r2.output
    assert "messages_embedded_repaired=3" in r2.output

    store = SessionStore(db_path=home / ".index" / "sessions.db", dim=384)
    store.init_schema()
    query_vec = HashingEmbedder(dim=384).embed("second user turn with list content")
    hits = store.search_vec(query_vec, top_k=3)
    store.close()
    assert hits and hits[0]["message_uuid"] == "u-3"
