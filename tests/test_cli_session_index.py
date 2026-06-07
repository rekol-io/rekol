"""Smoke tests for claude-session-index CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
from click.testing import CliRunner

from cache_helpers import cache_dir_for
from rekol.cli_session_index import main as cli_main
from rekol.embeddings import HashingEmbedder
from rekol.sessions.store import SessionStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


def _setup_home(tmp_path: Path, monkeypatch) -> Path:
    """Create a memory home with a fake projects dir holding the fixture.

    Uses the ``test-hashing`` embedder so the embedding path runs without
    loading the real sentence-transformers model. The transcript archive
    (default-ON) is isolated to a throwaway dir by the autouse conftest fixture
    (``XDG_DATA_HOME``), so these tests never touch the real machine archive.
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
    assert (cache_dir_for(home) / "sessions.db").exists()


def test_session_index_incremental_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "fake-projects" / "proj-a"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURE, projects / "session.jsonl")

    # Archive on, pointed at a hermetic tmp dir so the test never writes a real
    # archive. The repoint means ingest reads from here, so files_seen is keyed on
    # the ARCHIVE path — the second incremental run must still skip it (self-heal).
    archive = tmp_path / "archive"
    monkeypatch.setenv("MEMORY_HOME", str(home))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))
    (home / "memory.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\n"
        "embedding_model: test-hashing\n"
        "archive_enabled: true\n"
    )

    runner = CliRunner()
    setup_result = runner.invoke(cli_main, ["--full"])
    assert setup_result.exit_code == 0, setup_result.output
    # The session was archived and (after --full) re-keyed in files_seen by its
    # archive path.
    assert (archive / "proj-a" / "session.jsonl").exists()

    result = runner.invoke(cli_main, ["--incremental"])
    assert result.exit_code == 0, result.output
    # Incremental against the archive-keyed files_seen → mtime+size skip, no inserts.
    assert "messages_inserted=0" in result.output
    assert "files_skipped_unchanged=1" in result.output


def test_session_index_leaves_fts_in_sync_after_full(tmp_path: Path, monkeypatch) -> None:
    """C5 (FTS-at-build): a --full reingest rebuilds the FTS index from `messages`,
    so the on-disk index is consistent — no orphaned postings, no unindexed rows.
    """
    home = _setup_home(tmp_path, monkeypatch)
    runner = CliRunner()
    assert runner.invoke(cli_main, ["--full"]).exit_code == 0

    store = SessionStore(db_path=cache_dir_for(home) / "sessions.db", dim=384)
    store.init_schema()
    try:
        orphaned, unindexed = store.fts_consistency()
        assert (orphaned, unindexed) == (0, 0)
        assert store.fts_is_in_sync() is True
    finally:
        store.close()


def test_session_index_keeps_fts_in_sync_after_incremental(tmp_path: Path, monkeypatch) -> None:
    """C5: an incremental ingest of a brand-new file relies on the sync triggers to
    keep FTS current — the new messages must be indexed, not just inserted.
    """
    home = _setup_home(tmp_path, monkeypatch)
    runner = CliRunner()
    assert runner.invoke(cli_main, ["--full"]).exit_code == 0

    # Add a second transcript and ingest it incrementally (no --full rebuild).
    projects = tmp_path / "fake-projects" / "proj-b"
    projects.mkdir(parents=True)
    (projects / "session.jsonl").write_text(
        '{"parentUuid":null,"type":"user","message":{"role":"user",'
        '"content":"a fresh incremental message about kubernetes"},'
        '"uuid":"inc-1","timestamp":"2026-04-24T01:29:01.303Z",'
        '"sessionId":"inc-s","cwd":"/tmp/repoX"}\n'
    )
    res = runner.invoke(cli_main, ["--incremental"])
    assert res.exit_code == 0, res.output

    store = SessionStore(db_path=cache_dir_for(home) / "sessions.db", dim=384)
    store.init_schema()
    try:
        assert store.fts_is_in_sync() is True
        # The incrementally-added message is reachable by keyword (trigger fired).
        assert len(store.search_fts("kubernetes", top_k=5)) == 1
    finally:
        store.close()


def test_session_index_embeds_by_default(tmp_path: Path, monkeypatch) -> None:
    """Embedding is on by default, so a full ingest must populate the vector
    index — transcript search is semantic, not keyword-only.
    """
    home = _setup_home(tmp_path, monkeypatch)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--full"])
    assert result.exit_code == 0, result.output
    assert "messages_inserted=3" in result.output

    store = SessionStore(db_path=cache_dir_for(home) / "sessions.db", dim=384)
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

    store = SessionStore(db_path=cache_dir_for(home) / "sessions.db", dim=384)
    store.init_schema()
    query_vec = HashingEmbedder(dim=384).embed("second user turn with list content")
    hits = store.search_vec(query_vec, top_k=3)
    store.close()
    assert hits == []


def test_session_index_errors_on_embedding_dim_mismatch(tmp_path: Path, monkeypatch) -> None:
    """A sessions.db built at one embedding dimension must not be silently
    re-indexed under a different-dim model: the CLI should fail fast with an
    actionable message rather than crash on the first vector insert.
    """
    home = _setup_home(tmp_path, monkeypatch)  # config uses test-hashing => 384-dim
    db_path = cache_dir_for(home) / "sessions.db"

    # Pre-build the index at a deliberately different dimension (8).
    store = SessionStore(db_path=db_path, dim=8)
    store.init_schema()
    rid = store.insert_message(
        dict(
            session_id="s-1",
            message_uuid="u-1",
            parent_uuid=None,
            role="user",
            content="hello",
            cwd="/tmp/repo",
            timestamp_iso="2026-05-28T20:00:00Z",
            timestamp_unix=1748462400,
            jsonl_path="/fake.jsonl",
            line_number=2,
        )
    )
    with store.conn:
        store.upsert_embedding_no_commit(rid, np.zeros(8, dtype=np.float32))
    store.close()

    result = CliRunner().invoke(cli_main, ["--full"])
    assert result.exit_code == 2, result.output
    assert "8-dim" in result.output and "384-dim" in result.output


def test_session_index_recreates_empty_stale_vec_table(tmp_path: Path, monkeypatch) -> None:
    """An empty vec table at a stale width (e.g. left by a --no-embed run under a
    non-default model) must NOT force a destructive rebuild — it is recreated at
    the model's width and embedding proceeds.
    """
    home = _setup_home(tmp_path, monkeypatch)  # test-hashing => 384-dim
    db_path = cache_dir_for(home) / "sessions.db"

    # Pre-create an EMPTY index at a different width (no embeddings written).
    store = SessionStore(db_path=db_path, dim=8)
    store.init_schema()
    assert store.count_embeddings() == 0
    store.close()

    result = CliRunner().invoke(cli_main, ["--full"])
    assert result.exit_code == 0, result.output
    assert "messages_inserted=3" in result.output

    store = SessionStore(db_path=db_path, dim=384)
    store.init_schema()
    query_vec = HashingEmbedder(dim=384).embed("second user turn with list content")
    hits = store.search_vec(query_vec, top_k=3)
    store.close()
    assert hits and hits[0]["message_uuid"] == "u-3"


def test_search_errors_on_embedding_dim_mismatch(tmp_path: Path, monkeypatch) -> None:
    """The read path must surface the same actionable dim-mismatch message as
    ingest, not let sqlite-vec raise a cryptic width error mid-query.
    """
    from rekol.cli_search import main as search_main

    home = _setup_home(tmp_path, monkeypatch)  # test-hashing => 384-dim
    db_path = cache_dir_for(home) / "sessions.db"

    # A populated index at a different width than the configured model.
    store = SessionStore(db_path=db_path, dim=8)
    store.init_schema()
    rid = store.insert_message(
        dict(
            session_id="s-1",
            message_uuid="u-1",
            parent_uuid=None,
            role="user",
            content="hello",
            cwd="/tmp/repo",
            timestamp_iso="2026-05-28T20:00:00Z",
            timestamp_unix=1748462400,
            jsonl_path="/fake.jsonl",
            line_number=2,
        )
    )
    with store.conn:
        store.upsert_embedding_no_commit(rid, np.zeros(8, dtype=np.float32))
    store.close()

    result = CliRunner().invoke(search_main, ["--source", "sessions", "hello"])
    assert result.exit_code == 2, result.output
    assert "8-dim" in result.output and "384-dim" in result.output


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

    store = SessionStore(db_path=cache_dir_for(home) / "sessions.db", dim=384)
    store.init_schema()
    query_vec = HashingEmbedder(dim=384).embed("second user turn with list content")
    hits = store.search_vec(query_vec, top_k=3)
    store.close()
    assert hits and hits[0]["message_uuid"] == "u-3"


def _write_session_with_cwd(path: Path, *, session_id: str, cwd: str, text: str) -> None:
    """Write a one-message transcript carrying a real `cwd` (the exclude target)."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "type": "user",
        "uuid": f"{session_id}-u1",
        "sessionId": session_id,
        "timestamp": "2026-05-01T10:00:00Z",
        "cwd": cwd,
        "message": {"role": "user", "content": text},
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_excluded_project_not_indexed_when_archive_soft_fails(tmp_path: Path, monkeypatch) -> None:
    """SECURITY: when archive-sync OSErrors and falls back to live, ingest must NOT
    index excluded/secret projects. ingest_directory has no exclude awareness, so
    the fallback must either filter or refuse — never silently index a secret
    project the user excluded.
    """
    import rekol.cli_session_index as cli_mod

    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "fake-projects"
    _write_session_with_cwd(
        projects / "-Users-x-secret" / "s-secret.jsonl",
        session_id="s-secret",
        cwd="/Users/x/secret",
        text="this is a SECRET project transcript",
    )
    _write_session_with_cwd(
        projects / "-Users-x-public" / "s-public.jsonl",
        session_id="s-public",
        cwd="/Users/x/public",
        text="this is a public project transcript",
    )
    archive = tmp_path / "archive"
    monkeypatch.setenv("MEMORY_HOME", str(home))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))
    (home / "memory.config.yaml").write_text(
        f"claude_projects_dir: {projects}\n"
        "embedding_model: test-hashing\n"
        "archive_enabled: true\n"
        "exclude_paths:\n"
        "  - '*/secret*'\n"
    )

    # Force archive-sync to OSError so the fallback path is exercised.
    def boom(*args, **kwargs):
        raise OSError("simulated unwritable archive dir")

    monkeypatch.setattr(cli_mod, "archive_directory", boom)
    result = CliRunner().invoke(cli_main, ["--full"])
    assert result.exit_code == 0, result.output

    db_path = cache_dir_for(home) / "sessions.db"
    if not db_path.exists():
        # No-ingest degradation is an acceptable exclude-safe fallback: nothing
        # indexed at all, so no secret leaked. Done.
        return
    store = SessionStore(db_path=db_path, dim=384)
    store.init_schema()
    try:
        rows = store.conn.execute("SELECT cwd, content FROM messages").fetchall()
    finally:
        store.close()
    blob = " ".join(f"{r['cwd']} {r['content']}" for r in rows)
    # The excluded/secret project must NOT be in the index.
    assert "secret" not in blob.lower(), blob


def test_session_index_uses_archive_backfill_once(tmp_path: Path, monkeypatch) -> None:
    """main() must call the hardened, tested archive.backfill_once (not a private
    duplicate). We assert the consolidated entry point runs and writes the marker.
    """
    import rekol.cli_session_index as cli_mod
    from rekol.sessions.archive import BACKFILL_MARKER_FILENAME

    _setup_home(tmp_path, monkeypatch)
    archive = tmp_path / "archive"
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))

    calls: list[str] = []
    real_backfill_once = cli_mod.backfill_once

    def spy(store, archive_dir, exclude_patterns):
        calls.append("called")
        return real_backfill_once(store, archive_dir, exclude_patterns)

    monkeypatch.setattr(cli_mod, "backfill_once", spy)
    result = CliRunner().invoke(cli_main, ["--full"])
    assert result.exit_code == 0, result.output
    assert calls == ["called"]
    assert (archive / BACKFILL_MARKER_FILENAME).exists()


def test_session_index_survives_corrupt_db_during_backfill(tmp_path: Path, monkeypatch) -> None:
    """The backfill step must never crash the SessionEnd hook: a corrupt/locked
    sessions.db raises sqlite3.DatabaseError (NOT OSError), which the old private
    _maybe_backfill_once did not catch. The run must degrade to a non-fatal notice
    and exit 0, still ingesting the live session.
    """
    import sqlite3

    import rekol.cli_session_index as cli_mod

    _setup_home(tmp_path, monkeypatch)
    archive = tmp_path / "archive"
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))

    def boom(store, archive_dir, exclude_patterns):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(cli_mod, "backfill_once", boom)
    result = CliRunner().invoke(cli_main, ["--full"])
    # Exit 0 despite the backfill failing — archiving/backfill never blocks indexing.
    assert result.exit_code == 0, result.output
    # The live session was still ingested.
    assert "messages_inserted=3" in result.output
    # A non-fatal degradation notice was emitted (not a traceback).
    assert "backfill" in result.output.lower()
