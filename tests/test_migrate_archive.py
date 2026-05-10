"""Tests for archiver: move originals, write migration marker."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from memory_tools.migrate.archive import (
    MIGRATION_MARKER_NAME,
    archive_file,
    write_retirement_pointer,
)
from memory_tools.migrate.discover import (
    LegacyFile,
    has_migration_marker,
    is_retirement_pointer,
)


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


def test_write_retirement_pointer_writes_marker_and_removes_legacy(
    tmp_path: Path,
) -> None:
    """New convention: write hidden ``.MIGRATED`` marker; remove pre-existing
    ``MEMORY.md`` (which Claude Code's autoMemory would otherwise inject)."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("# Original index\n\nReal content.\n")
    write_retirement_pointer(memory_dir, memory_home=Path("/fake/memory"))
    marker = memory_dir / MIGRATION_MARKER_NAME
    assert marker.is_file()
    assert "/fake/memory" in marker.read_text()
    # MEMORY.md was removed so autoMemory has nothing to inject
    assert not (memory_dir / "MEMORY.md").exists()
    assert has_migration_marker(memory_dir) is True


def test_write_retirement_pointer_idempotent(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    write_retirement_pointer(memory_dir, memory_home=Path("/fake"))
    marker = memory_dir / MIGRATION_MARKER_NAME
    first = marker.read_text()
    # Second call should not error and should not double-write
    write_retirement_pointer(memory_dir, memory_home=Path("/fake"))
    assert marker.read_text() == first


def test_legacy_memory_md_retirement_string_still_recognized(tmp_path: Path) -> None:
    """Backward-compat: a dir migrated by an old version of this tool wrote
    ``MEMORY.md`` with the ``RETIRED — migrated`` string.  Re-runs against such
    a dir must still skip it."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(
        "# RETIRED — migrated to memory-tools $MEMORY_HOME (2026-04-01)\n\n"
        "All cross-session context...\n"
    )
    assert is_retirement_pointer(memory_dir / "MEMORY.md") is True
    assert has_migration_marker(memory_dir) is True
