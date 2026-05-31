"""Tests for memory-migrate CLI subcommands."""

from __future__ import annotations

import shutil
from pathlib import Path

from click.testing import CliRunner

from memory_tools.cli_migrate import main

FIXTURES = Path(__file__).parent / "fixtures" / "legacy-project" / "memory"


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    src_parent = tmp_path / "proj"
    src_parent.mkdir()
    shutil.copytree(FIXTURES, src_parent / "memory")
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)
    (memory_home / "INDEX.md").write_text("# Index\n")
    return src_parent, memory_home


def test_cli_repo_dry_run(tmp_path: Path, monkeypatch) -> None:
    src_parent, memory_home = _setup(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "repo",
            str(src_parent),
            "--dry-run",
            "--no-llm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "would migrate" in result.output.lower()
    # No files actually written
    assert not any((memory_home / "topics").iterdir())


def test_cli_repo_commit(tmp_path: Path, monkeypatch) -> None:
    src_parent, memory_home = _setup(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "repo",
            str(src_parent),
            "--commit",
            "--no-llm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (memory_home / "topics" / "proj-alpha.md").is_file()
    assert (memory_home / "when" / "when-proj-foo.md").is_file()


def test_cli_auto_no_legacy_is_noop(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    memory_home = tmp_path / "MEMORY_HOME"
    (memory_home / "topics").mkdir(parents=True)
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    runner = CliRunner()
    result = runner.invoke(main, ["auto", "--commit", "--no-llm", "--quiet"])
    assert result.exit_code == 0, result.output
    assert "nothing to migrate" in result.output.lower() or result.output.strip() == ""


def test_cli_auto_finds_project_dirs(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-Users-x-proj-a" / "memory"
    proj.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "project_alpha.md", proj / "project_alpha.md")
    shutil.copyfile(FIXTURES / "MEMORY.md", proj / "MEMORY.md")
    monkeypatch.setenv("HOME", str(home))
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True)
    (memory_home / "INDEX.md").write_text("# Index\n")
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))

    runner = CliRunner()
    result = runner.invoke(main, ["auto", "--commit", "--no-llm"])
    assert result.exit_code == 0, result.output
    # Classified as topic (type: project → topic)
    # Slug becomes -Users-x-proj-a
    assert any((memory_home / "topics").iterdir())


def test_cli_requires_memory_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    runner = CliRunner()
    result = runner.invoke(main, ["auto", "--commit"])
    assert result.exit_code != 0
    assert "MEMORY_HOME" in result.output
