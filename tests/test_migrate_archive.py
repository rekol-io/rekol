"""Tests for archiver: move originals, write retirement pointer."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from memory_tools.migrate.archive import (
    archive_file,
    write_retirement_pointer,
)
from memory_tools.migrate.discover import LegacyFile, is_retirement_pointer


def _mk_file(tmp_path: Path, name: str, body: str = "body") -> LegacyFile:
    src = tmp_path / "proj" / "memory"
    src.mkdir(parents=True, exist_ok=True)
    f = src / name
    f.write_text(body)
    return LegacyFile(source_path=f, source_root=src, project_slug="proj")


def test_archive_file_moves_into_old_memory_archive(tmp_path: Path) -> None:
    lf = _mk_file(tmp_path, "x.md", "original")
    archive_file(lf)
    archived = lf.source_root / "old-memory-archive" / "x.md"
    assert archived.is_file()
    assert archived.read_text() == "original"
    assert not lf.source_path.exists()


def test_archive_file_creates_archive_dir(tmp_path: Path) -> None:
    lf = _mk_file(tmp_path, "x.md")
    assert not (lf.source_root / "old-memory-archive").exists()
    archive_file(lf)
    assert (lf.source_root / "old-memory-archive").is_dir()


def test_archive_file_collision_appends_suffix(tmp_path: Path) -> None:
    lf1 = _mk_file(tmp_path, "x.md", "first")
    archive_file(lf1)
    # Write a second x.md and archive it
    lf2 = LegacyFile(
        source_path=lf1.source_root / "x.md",
        source_root=lf1.source_root,
        project_slug="proj",
    )
    lf2.source_path.write_text("second")
    archive_file(lf2)
    names = sorted((lf1.source_root / "old-memory-archive").iterdir())
    # Expect x.md and x-1.md
    assert [n.name for n in names] == ["x-1.md", "x.md"]


def test_write_retirement_pointer_replaces_memory_md(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("# Original index\n\nReal content.\n")
    write_retirement_pointer(memory_dir, memory_home=Path("/fake/memory"))
    assert is_retirement_pointer(memory_dir / "MEMORY.md") is True
    assert "/fake/memory" in (memory_dir / "MEMORY.md").read_text()


def test_write_retirement_pointer_idempotent(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    write_retirement_pointer(memory_dir, memory_home=Path("/fake"))
    first = (memory_dir / "MEMORY.md").read_text()
    # Second call should not error and should not double-append
    write_retirement_pointer(memory_dir, memory_home=Path("/fake"))
    assert (memory_dir / "MEMORY.md").read_text() == first
