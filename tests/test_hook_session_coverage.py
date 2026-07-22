"""Tests for ``rekol _hook session-coverage`` (#123 part 2).

The banner rides on the SessionStart injection and must NEVER break it: any error
prints nothing and exits 0. It reads the invisible-file count the indexer persists
to the cache and prints one line only when non-zero.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from rekol.cli_hooks import session_coverage
from rekol.config import SKIP_MANIFEST_NAME, load_config


def _seed_home(home: Path, monkeypatch) -> Path:
    """A sandboxed REKOL_HOME with a minimal config; returns the resolved index_dir."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "rekol.config.yaml").write_text("embedding_model: test-hashing\n")
    monkeypatch.setenv("REKOL_HOME", str(home))
    index_dir = load_config().index_dir
    index_dir.mkdir(parents=True, exist_ok=True)
    return index_dir


def _write_manifest(index_dir: Path, contents: str) -> None:
    (index_dir / SKIP_MANIFEST_NAME).write_text(contents)


def test_silent_when_manifest_absent(tmp_path: Path, monkeypatch) -> None:
    _seed_home(tmp_path, monkeypatch)  # no manifest written
    result = CliRunner().invoke(session_coverage, [])
    assert result.exit_code == 0
    assert result.output == ""


def test_silent_when_count_zero(tmp_path: Path, monkeypatch) -> None:
    index_dir = _seed_home(tmp_path, monkeypatch)
    _write_manifest(index_dir, '{"count": 0, "paths": []}')
    result = CliRunner().invoke(session_coverage, [])
    assert result.exit_code == 0
    assert result.output == ""


def test_warns_when_count_nonzero(tmp_path: Path, monkeypatch) -> None:
    index_dir = _seed_home(tmp_path, monkeypatch)
    _write_manifest(index_dir, '{"count": 3, "paths": ["topics/a.md"]}')
    result = CliRunner().invoke(session_coverage, [])
    assert result.exit_code == 0
    assert "3 memory files invisible to search" in result.output
    assert "rekol doctor" in result.output


def test_singular_noun_for_one_file(tmp_path: Path, monkeypatch) -> None:
    index_dir = _seed_home(tmp_path, monkeypatch)
    _write_manifest(index_dir, '{"count": 1}')
    result = CliRunner().invoke(session_coverage, [])
    assert "1 memory file invisible" in result.output


def test_never_raises_on_garbage_manifest(tmp_path: Path, monkeypatch) -> None:
    index_dir = _seed_home(tmp_path, monkeypatch)
    _write_manifest(index_dir, "not json {{{")
    result = CliRunner().invoke(session_coverage, [])
    assert result.exit_code == 0
    assert result.output == ""
