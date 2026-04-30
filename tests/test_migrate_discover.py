"""Tests for legacy memory discovery."""
from __future__ import annotations

from pathlib import Path

import pytest

from memory_tools.migrate.discover import (
    discover_auto_memory_sources,
    discover_files_in_dir,
    is_retirement_pointer,
)


FIXTURES = Path(__file__).parent / "fixtures"
LEGACY_DIR = FIXTURES / "legacy-project" / "memory"


def test_discover_files_returns_all_md_except_memory_md(tmp_path: Path) -> None:
    # Copy fixture to a writable temp dir so test can mutate later
    import shutil
    src = tmp_path / "memory"
    shutil.copytree(LEGACY_DIR, src)

    files = discover_files_in_dir(src)
    names = sorted(f.source_path.name for f in files)
    assert names == ["feedback_foo.md", "project_alpha.md"]
    # MEMORY.md excluded (it's the index, not content)


def test_discover_files_project_slug_is_parent_name(tmp_path: Path) -> None:
    import shutil
    src = tmp_path / "my-project" / "memory"
    shutil.copytree(LEGACY_DIR, src)

    files = discover_files_in_dir(src)
    assert all(f.project_slug == "my-project" for f in files)


def test_discover_files_skips_archive_subdir(tmp_path: Path) -> None:
    import shutil
    src = tmp_path / "memory"
    shutil.copytree(LEGACY_DIR, src)
    archive = src / "old-memory-archive"
    archive.mkdir()
    (archive / "old_file.md").write_text("---\nname: Old\ndescription: d\ntype: topic\n---\n\nbody")

    files = discover_files_in_dir(src)
    names = [f.source_path.name for f in files]
    assert "old_file.md" not in names


def test_is_retirement_pointer_true(tmp_path: Path) -> None:
    memory_md = tmp_path / "MEMORY.md"
    memory_md.write_text("# RETIRED — migrated to memory-tools $MEMORY_HOME (2026-04-29)\n")
    assert is_retirement_pointer(memory_md) is True


def test_is_retirement_pointer_false(tmp_path: Path) -> None:
    memory_md = tmp_path / "MEMORY.md"
    memory_md.write_text("# Some Project Memory\n\nReal content.\n")
    assert is_retirement_pointer(memory_md) is False


def test_is_retirement_pointer_missing_file(tmp_path: Path) -> None:
    assert is_retirement_pointer(tmp_path / "does-not-exist.md") is False


def test_discover_auto_memory_sources_empty_when_missing(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    sources = discover_auto_memory_sources()
    assert sources == []


def test_discover_auto_memory_sources_finds_project_dirs(tmp_path: Path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    projects_root = fake_home / ".claude" / "projects"
    (projects_root / "-Users-foo-proj-a" / "memory").mkdir(parents=True)
    (projects_root / "-Users-foo-proj-a" / "memory" / "MEMORY.md").write_text("# real\n")
    (projects_root / "-Users-foo-proj-a" / "memory" / "x.md").write_text("# x\n")
    (projects_root / "-Users-foo-proj-b" / "memory").mkdir(parents=True)
    (projects_root / "-Users-foo-proj-b" / "memory" / "MEMORY.md").write_text("# RETIRED — migrated to memory-tools\n")  # Retirement marker is the exact phrase discover.RETIREMENT_MARKER looks for.
    # proj-b is retired and should be skipped

    monkeypatch.setenv("HOME", str(fake_home))
    sources = discover_auto_memory_sources()
    names = sorted(s.name for s in sources)
    assert names == ["-Users-foo-proj-a"]
