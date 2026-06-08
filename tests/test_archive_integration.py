"""Integration tests for the durable-archive data-loss fix (#8).

The headline regression: archive a session, DELETE the live .jsonl, rebuild the
index from scratch, and assert the session is still searchable — proving the
archive (not the ephemeral live file) is the source of truth for rebuilds."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from click.testing import CliRunner

from cache_helpers import cache_dir_for
from rekol.cli_session_index import main as session_index_cmd
from rekol.sessions.store import SessionStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


def _write_one_row(path: Path, *, session_id: str, cwd: str) -> None:
    """Write a single-row transcript carrying a real `cwd` for exclude matching."""
    row = {
        "type": "user",
        "uuid": f"{session_id}-u1",
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": "2026-05-01T10:00:00Z",
        "message": {"role": "user", "content": "hello there from the test"},
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def _home(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "projects" / "proj-a"
    projects.mkdir(parents=True)
    live_jsonl = projects / "session.jsonl"
    shutil.copy(FIXTURE, live_jsonl)
    archive = tmp_path / "archive"
    monkeypatch.setenv("REKOL_HOME", str(home))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))
    (home / "rekol.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\nembedding_model: test-hashing\n"
    )
    return home, live_jsonl, archive


def test_session_survives_live_deletion_then_full_rebuild(tmp_path: Path, monkeypatch) -> None:
    home, live_jsonl, archive = _home(tmp_path, monkeypatch)
    runner = CliRunner()

    # 1. Index once — this archive-syncs the session, then ingests from archive.
    first = runner.invoke(session_index_cmd, ["--incremental"])
    assert first.exit_code == 0, first.output
    assert (archive / "proj-a" / "session.jsonl").exists()

    # 2. Claude Code "cleans up" the original — delete the live .jsonl.
    live_jsonl.unlink()
    assert not live_jsonl.exists()

    # 3. Wipe the cache (sessions.db) and do a FULL rebuild from scratch.
    sessions_db = cache_dir_for(home) / "sessions.db"
    if sessions_db.exists():
        sessions_db.unlink()
    rebuilt = runner.invoke(session_index_cmd, ["--full"])
    assert rebuilt.exit_code == 0, rebuilt.output

    # 4. The session is STILL searchable — rebuilt losslessly from the archive.
    store = SessionStore(db_path=sessions_db, dim=384)
    store.init_schema()
    try:
        hits = store.search_fts("hello there", top_k=5)
        assert any("hello" in h["content"] for h in hits), [h["content"] for h in hits]
    finally:
        store.close()


def test_unwritable_archive_falls_back_to_live_and_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """SOFT-FAIL: an archive dir that raises OSError on write must NOT block
    indexing — the run degrades to ingesting from live and exits 0 (the hook
    contract). Same cycle/commit as the wiring; not a separate task."""
    home, _live, archive = _home(tmp_path, monkeypatch)
    runner = CliRunner()

    import rekol.cli_session_index as session_mod

    def boom(live_root, archive_dir, exclude_patterns):
        raise OSError("simulated unwritable archive dir")

    monkeypatch.setattr(session_mod, "archive_directory", boom)
    result = runner.invoke(session_index_cmd, ["--incremental"])
    # Soft-fail: still indexes (from live), exit 0, with a non-fatal notice.
    assert result.exit_code == 0, result.output
    assert "degraded (non-fatal)" in result.output
    # The session was still ingested from live despite the archive failure.
    store = SessionStore(db_path=cache_dir_for(home) / "sessions.db", dim=384)
    store.init_schema()
    try:
        assert store.count_messages() > 0
    finally:
        store.close()


def test_excluded_project_never_archived_or_indexed(tmp_path: Path, monkeypatch) -> None:
    """End-to-end exclude: a project whose REAL cwd matches an exclude is neither
    archived nor indexed; a non-excluded project is both. Match is on cwd, not the
    slug folder (the design decision). Same cycle/commit as the wiring."""
    home = tmp_path / "memhome"
    home.mkdir()
    # On-disk slug folders are URL-encoded; the matchable path is the row cwd.
    secret = tmp_path / "projects" / "-Users-x-secret-project"
    public = tmp_path / "projects" / "-Users-x-public"
    secret.mkdir(parents=True)
    public.mkdir(parents=True)
    _write_one_row(secret / "s.jsonl", session_id="sess-secret", cwd="/Users/x/secret-project")
    _write_one_row(public / "p.jsonl", session_id="sess-public", cwd="/Users/x/public")
    archive = tmp_path / "archive"
    monkeypatch.setenv("REKOL_HOME", str(home))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))
    (home / "rekol.config.yaml").write_text(
        f"claude_projects_dir: {tmp_path / 'projects'}\n"
        "embedding_model: test-hashing\n"
        "exclude_paths:\n  - '*/secret-project*'\n"
    )

    result = CliRunner().invoke(session_index_cmd, ["--full"])
    assert result.exit_code == 0, result.output
    # Excluded project is neither archived nor indexed; the public one is both.
    assert not (archive / "-Users-x-secret-project").exists()
    assert (archive / "-Users-x-public" / "p.jsonl").exists()
    store = SessionStore(db_path=cache_dir_for(home) / "sessions.db", dim=384)
    store.init_schema()
    try:
        cwds = {r["cwd"] for r in store.conn.execute("SELECT DISTINCT cwd FROM messages")}
        assert not any(c and "secret-project" in c for c in cwds), cwds
    finally:
        store.close()
