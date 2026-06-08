"""Smoke + behavior tests for the `rekol archive` CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from rekol.cli_archive import main as archive_cmd
from rekol.sessions.archive import BACKFILL_MARKER_FILENAME

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


def _home_with_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "projects" / "proj-a"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURE, projects / "session.jsonl")
    archive = tmp_path / "archive"
    monkeypatch.setenv("REKOL_HOME", str(home))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))
    (home / "rekol.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\nembedding_model: test-hashing\n"
    )
    return home, archive


def test_archive_sync_copies_live_into_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, archive = _home_with_projects(tmp_path, monkeypatch)
    result = CliRunner().invoke(archive_cmd, [])
    assert result.exit_code == 0, result.output
    assert (archive / "proj-a" / "session.jsonl").exists()
    assert "files_copied=1" in result.output


def test_archive_disabled_is_a_clean_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, archive = _home_with_projects(tmp_path, monkeypatch)
    (home / "rekol.config.yaml").write_text(
        "archive_enabled: false\nembedding_model: test-hashing\n"
    )
    result = CliRunner().invoke(archive_cmd, [])
    assert result.exit_code == 0, result.output
    assert "archive_enabled=false" in result.output
    assert not archive.exists()


def test_archive_prune_clear_empties_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, archive = _home_with_projects(tmp_path, monkeypatch)
    CliRunner().invoke(archive_cmd, [])
    assert (archive / "proj-a" / "session.jsonl").exists()
    result = CliRunner().invoke(archive_cmd, ["--prune", "--clear"])
    assert result.exit_code == 0, result.output
    assert list(archive.glob("**/*.jsonl")) == []


def test_archive_command_registered_in_group() -> None:
    from rekol.cli import main as group

    assert "archive" in group.commands


def test_from_index_writes_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--from-index` runs the backfill and writes the one-time guard marker, so
    the auto-once path (session-index) never re-runs it."""
    _home, archive = _home_with_projects(tmp_path, monkeypatch)
    # Need a sessions.db to backfill from: build one with session-index first.
    from rekol.cli_session_index import main as session_index_cmd

    CliRunner().invoke(session_index_cmd, ["--full"])
    result = CliRunner().invoke(archive_cmd, ["--from-index"])
    assert result.exit_code == 0, result.output
    assert (archive / BACKFILL_MARKER_FILENAME).exists()
    assert "backfill sessions_reconstructed=" in result.output
