"""Smoke tests for claude-session-index CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

from click.testing import CliRunner

from memory_tools.cli_session_index import main as cli_main

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


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
