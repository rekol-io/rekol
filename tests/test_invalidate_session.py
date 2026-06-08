"""Tests for ``rekol invalidate`` as the user-facing FORGET (#64).

``rekol invalidate`` is polymorphic on its single positional argument:

* an existing file under ``MEMORY_HOME`` -> the curated-memory soft-invalidate
  (covered in ``test_cli.py``; the dispatch is re-checked here).
* anything else -> a SESSION id -> the FORGET: remove that session from rekol's
  transcript index (``sessions.db``) AND the durable archive, so a re-run of
  ``session-index`` reading the archive cannot resurrect it.

Granularity is per-session — the unit the index keys on (``session_id``) and the
unit the archive stores (one ``.jsonl`` per session). The honest caveat is part
of the contract: forget removes from rekol, it does NOT delete Claude Code's live
``.jsonl`` (Claude owns it), so this is asserted in the help/output too.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from click.testing import CliRunner

from cache_helpers import cache_dir_for
from rekol.cli_invalidate import main as invalidate_main
from rekol.cli_session_index import main as session_index_main
from rekol.sessions.archive import MANIFEST_FILENAME, load_manifest
from rekol.sessions.store import SessionStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"
FIXTURE_SESSION_ID = "s-1"  # the sessionId carried by every row in the fixture


def _setup_indexed_session(
    tmp_path: Path, monkeypatch, *, session_id: str = FIXTURE_SESSION_ID
) -> tuple[Path, Path, Path]:
    """Build an index + archive from the fixture, then drop the live transcript.

    Mirrors the real FORGET scenario: the durable archive is the source of truth a
    rebuild reads from, and the live ``~/.claude/projects`` original may already be
    gone (Claude Code rotates/compacts it). Returns ``(home, archive, projects)``.
    """
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "fake-projects" / "proj-a"
    projects.mkdir(parents=True)
    live_jsonl = projects / "session.jsonl"
    shutil.copy(FIXTURE, live_jsonl)
    archive = tmp_path / "archive"

    monkeypatch.setenv("MEMORY_HOME", str(home))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))
    (home / "memory.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\n"
        "embedding_model: test-hashing\n"
        "archive_enabled: true\n"
    )

    runner = CliRunner()
    result = runner.invoke(session_index_main, ["--full"])
    assert result.exit_code == 0, result.output
    assert "messages_inserted=3" in result.output

    # The archive is now the durable source of truth. Remove the live original so a
    # re-index reads only the archive — the realistic forget setting (Claude Code's
    # live .jsonl may already be gone; the archive is what would resurrect it).
    live_jsonl.unlink()
    return home, archive, projects


def _session_in_index(home: Path, session_id: str) -> bool:
    store = SessionStore(db_path=cache_dir_for(home) / "sessions.db", dim=384)
    store.init_schema()
    try:
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (session_id,)
        ).fetchone()
        return int(row["n"]) > 0
    finally:
        store.close()


def _archive_files(archive: Path) -> list[Path]:
    return sorted(archive.glob("**/*.jsonl"))


def test_invalidate_session_removes_from_index(tmp_path: Path, monkeypatch) -> None:
    """Forgetting a session deletes its message rows from ``sessions.db``."""
    home, _archive, _projects = _setup_indexed_session(tmp_path, monkeypatch)
    assert _session_in_index(home, FIXTURE_SESSION_ID) is True

    result = CliRunner().invoke(invalidate_main, [FIXTURE_SESSION_ID])
    assert result.exit_code == 0, result.output
    assert _session_in_index(home, FIXTURE_SESSION_ID) is False


def test_invalidate_session_removes_from_archive(tmp_path: Path, monkeypatch) -> None:
    """Forgetting a session removes its archived ``.jsonl`` AND its manifest entry,
    so a later archive-sync treats it as gone (not a false 'unchanged' skip)."""
    home, archive, _projects = _setup_indexed_session(tmp_path, monkeypatch)
    assert _archive_files(archive), "fixture should have produced an archived file"
    assert (archive / MANIFEST_FILENAME).exists()

    result = CliRunner().invoke(invalidate_main, [FIXTURE_SESSION_ID])
    assert result.exit_code == 0, result.output

    # No archived transcript carries the forgotten session id any more.
    for archived in _archive_files(archive):
        for raw in archived.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            row = json.loads(raw)
            assert row.get("sessionId") != FIXTURE_SESSION_ID, archived

    # And no manifest entry points at a file for the forgotten session.
    manifest = load_manifest(archive)
    for key in manifest:
        assert FIXTURE_SESSION_ID not in Path(key).name


def test_invalidate_session_survives_reindex_no_resurrect(tmp_path: Path, monkeypatch) -> None:
    """The load-bearing guarantee: after forget, a re-run of ``session-index``
    (reading the archive, the live original gone) does NOT bring the session back.
    """
    home, _archive, _projects = _setup_indexed_session(tmp_path, monkeypatch)

    forget = CliRunner().invoke(invalidate_main, [FIXTURE_SESSION_ID])
    assert forget.exit_code == 0, forget.output
    assert _session_in_index(home, FIXTURE_SESSION_ID) is False

    # Re-run the indexer. With the archived copy and index rows gone (and the live
    # original removed), nothing can resurrect the forgotten session.
    reindex = CliRunner().invoke(session_index_main, ["--full"])
    assert reindex.exit_code == 0, reindex.output
    assert _session_in_index(home, FIXTURE_SESSION_ID) is False


def test_invalidate_session_unknown_id_is_a_clean_error(tmp_path: Path, monkeypatch) -> None:
    """Forgetting a session id absent from index AND archive is a loud, non-zero
    error — never a silent no-op that hides a typo'd session id."""
    home, _archive, _projects = _setup_indexed_session(tmp_path, monkeypatch)

    result = CliRunner().invoke(invalidate_main, ["no-such-session-id"])
    assert result.exit_code != 0, result.output
    assert "no-such-session-id" in result.output


def test_invalidate_session_caveat_in_output(tmp_path: Path, monkeypatch) -> None:
    """Honest caveat (spec §Forget): forget removes from rekol, it does NOT delete
    Claude Code's live ``.jsonl`` — the output must say so."""
    home, _archive, _projects = _setup_indexed_session(tmp_path, monkeypatch)

    result = CliRunner().invoke(invalidate_main, [FIXTURE_SESSION_ID])
    assert result.exit_code == 0, result.output
    lowered = result.output.lower()
    assert "claude" in lowered and ".jsonl" in lowered


def test_invalidate_help_documents_session_and_caveat() -> None:
    """The command help documents the per-session forget AND the honest caveat,
    so the contract is discoverable without reading the source."""
    result = CliRunner().invoke(invalidate_main, ["--help"])
    assert result.exit_code == 0, result.output
    lowered = result.output.lower()
    assert "session" in lowered
    assert ".jsonl" in lowered  # the Claude-owns-the-transcript caveat


def test_invalidate_still_soft_invalidates_a_memory_file(tmp_path: Path, monkeypatch) -> None:
    """Dispatch regression: an existing file under MEMORY_HOME still routes to the
    curated-memory soft-invalidate (frontmatter ``invalidated_at``), unchanged."""
    import frontmatter

    home = tmp_path / "memhome"
    (home / "topics").mkdir(parents=True)
    target = home / "topics" / "prometheus.md"
    target.write_text(
        "---\ntype: topic\ncreated: 2026-01-01\nupdated: 2026-01-01\n---\n\nA fact.\n"
    )
    monkeypatch.setenv("MEMORY_HOME", str(home))

    result = CliRunner().invoke(invalidate_main, [str(target), "--reason", "stale"])
    assert result.exit_code == 0, result.output
    post = frontmatter.load(target)
    assert post.metadata.get("invalidated_at"), post.metadata
    assert target.exists()
