"""Tests for archiver: move originals, write migration marker."""

from __future__ import annotations

from pathlib import Path

from rekol.migrate.archive import (
    MIGRATION_MARKER_NAME,
    archive_file,
    write_retirement_pointer,
)
from rekol.migrate.discover import (
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


def test_write_retirement_pointer_archives_legacy_memory_md_never_deletes_it(
    tmp_path: Path,
) -> None:
    """A real ``MEMORY.md`` must be MOVED into the archive, not unlinked.

    INVERTED (QA, 2026-11-06). This test previously wrote
    ``"# Original index\n\nReal content.\n"`` and asserted the file was **gone**
    — so the data loss was not a slip, it was pinned by a test.

    The original intent was to stop Claude Code's autoMemory injecting a
    *tombstone* written by an older version of this tool. But both callers sit
    behind `has_migration_marker`, which already treats an old-format tombstone
    as retired, so a real tombstone never reaches here: everything the unlink
    could delete was a user's own file. Moving it stops the injection just as
    well, because autoMemory cannot inject what is no longer at that path.
    """
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    body = "# Original index\n\nReal content.\n"
    (memory_dir / "MEMORY.md").write_text(body)

    archived = write_retirement_pointer(memory_dir, memory_home=Path("/fake/memory"))

    marker = memory_dir / MIGRATION_MARKER_NAME
    assert marker.is_file()
    assert "/fake/memory" in marker.read_text()
    assert has_migration_marker(memory_dir) is True

    # Gone from the injection path...
    assert not (memory_dir / "MEMORY.md").exists()
    # ...but byte-identical in the archive, and the caller is told where.
    assert archived is not None
    assert archived == memory_dir / "old-memory-archive" / "MEMORY.md"
    assert archived.is_file()
    assert archived.read_text() == body


def test_archiving_a_legacy_pointer_never_overwrites_an_earlier_copy(
    tmp_path: Path,
) -> None:
    """A second migration must not clobber the first archived MEMORY.md.

    The archive holds the only byte-complete original, so one silent overwrite is
    one unrecoverable file.
    """
    memory_dir = tmp_path / "memory"
    (memory_dir / "old-memory-archive").mkdir(parents=True)
    (memory_dir / "old-memory-archive" / "MEMORY.md").write_text("FIRST\n")
    (memory_dir / "MEMORY.md").write_text("SECOND\n")

    archived = write_retirement_pointer(memory_dir, memory_home=Path("/fake/memory"))

    assert archived == memory_dir / "old-memory-archive" / "MEMORY-1.md"
    assert archived.read_text() == "SECOND\n"
    assert (memory_dir / "old-memory-archive" / "MEMORY.md").read_text() == "FIRST\n"


def test_write_retirement_pointer_returns_none_when_there_is_no_legacy_pointer(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    assert write_retirement_pointer(memory_dir, memory_home=Path("/fake/memory")) is None


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


def test_marker_never_promises_time_based_deletion(tmp_path: Path) -> None:
    """Lock the product text that caused the defect.

    The marker used to say "safe to delete after 1 week" while migration strips
    unrecognised legacy frontmatter that then exists ONLY in that archive —
    a live instruction to destroy the only complete copy. Cheap to lock, and
    this is exactly the string that must not drift back.
    """
    from rekol.migrate.archive import MIGRATION_MARKER_NAME, write_retirement_pointer

    memory_dir = tmp_path / "legacy" / "memory"
    memory_dir.mkdir(parents=True)
    write_retirement_pointer(memory_dir, memory_home=tmp_path / "HOME")

    text = (memory_dir / MIGRATION_MARKER_NAME).read_text()
    assert "safe to delete" not in text.lower(), "no calendar-based deletion promise"
    assert "1 week" not in text
    assert "KEEP" in text, "must tell the user to retain the originals"
    assert "UNRECOGNISED" in text, "must say why: dropped keys survive only here"
