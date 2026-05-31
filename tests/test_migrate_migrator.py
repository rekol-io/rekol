"""End-to-end tests for the migrator orchestrator."""

from __future__ import annotations

import shutil
from pathlib import Path

from memory_tools.migrate.archive import MIGRATION_MARKER_NAME
from memory_tools.migrate.migrator import (
    migrate_dir,
)

FIXTURES = Path(__file__).parent / "fixtures" / "legacy-project" / "memory"


def _setup_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Returns (source_root_parent, memory_home)."""
    src_parent = tmp_path / "proj"
    src_parent.mkdir()
    shutil.copytree(FIXTURES, src_parent / "memory")
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)
    (memory_home / "INDEX.md").write_text("# Index\n")
    return src_parent, memory_home


def test_migrate_dir_commits_heuristic_files(tmp_path: Path) -> None:
    src_parent, memory_home = _setup_fixture(tmp_path)
    report = migrate_dir(
        source_dir=src_parent / "memory",
        memory_home=memory_home,
        dry_run=False,
        allow_llm=False,
    )
    # Two fixture files: project_alpha.md (heuristic→topic), feedback_foo.md (heuristic→when)
    assert report.migrated == 2
    assert report.by_heuristic == 2
    assert report.by_llm == 0
    assert (memory_home / "topics" / "proj-alpha.md").is_file()
    assert (memory_home / "when" / "when-proj-foo.md").is_file()
    # Migration marker (hidden, not auto-injected by Claude Code's autoMemory)
    marker = src_parent / "memory" / MIGRATION_MARKER_NAME
    assert marker.is_file()
    assert str(memory_home) in marker.read_text()
    # Archive
    assert (src_parent / "memory" / "old-memory-archive" / "project_alpha.md").is_file()
    assert (src_parent / "memory" / "old-memory-archive" / "feedback_foo.md").is_file()


def test_migrate_dir_dry_run_writes_nothing(tmp_path: Path) -> None:
    src_parent, memory_home = _setup_fixture(tmp_path)
    report = migrate_dir(
        source_dir=src_parent / "memory",
        memory_home=memory_home,
        dry_run=True,
        allow_llm=False,
    )
    assert report.would_migrate == 2
    assert report.migrated == 0
    # Nothing written to MEMORY_HOME
    assert not any((memory_home / "topics").iterdir())
    assert not any((memory_home / "when").iterdir())
    # Archive not created
    assert not (src_parent / "memory" / "old-memory-archive").exists()
    # Migration marker not written; original MEMORY.md untouched
    assert not (src_parent / "memory" / MIGRATION_MARKER_NAME).exists()
    assert (src_parent / "memory" / "MEMORY.md").is_file()


def test_migrate_dir_idempotent_skips_retired(tmp_path: Path) -> None:
    src_parent, memory_home = _setup_fixture(tmp_path)
    migrate_dir(
        source_dir=src_parent / "memory", memory_home=memory_home, dry_run=False, allow_llm=False
    )
    # Second run — should be a no-op (pointer already set, no files to migrate)
    report = migrate_dir(
        source_dir=src_parent / "memory", memory_home=memory_home, dry_run=False, allow_llm=False
    )
    assert report.migrated == 0
    assert report.skipped_retired == 1


def test_migrate_dir_collision_appends_suffix(tmp_path: Path) -> None:
    src_parent, memory_home = _setup_fixture(tmp_path)
    # Pre-create a target file that would collide
    (memory_home / "topics" / "proj-alpha.md").write_text("pre-existing")
    report = migrate_dir(
        source_dir=src_parent / "memory", memory_home=memory_home, dry_run=False, allow_llm=False
    )
    assert report.migrated == 2
    # Migrated file landed at proj-alpha-2.md (suffix-1 reserved for actual collision)
    collided = memory_home / "topics" / "proj-alpha-2.md"
    assert collided.is_file()
    # Original pre-existing file untouched
    assert (memory_home / "topics" / "proj-alpha.md").read_text() == "pre-existing"


def test_migrate_dir_writes_frontmatter_and_body(tmp_path: Path) -> None:
    src_parent, memory_home = _setup_fixture(tmp_path)
    migrate_dir(
        source_dir=src_parent / "memory", memory_home=memory_home, dry_run=False, allow_llm=False
    )
    out = (memory_home / "when" / "when-proj-foo.md").read_text()
    assert out.startswith("---\n")
    assert "type: when" in out
    assert "Foo feedback body content" in out


def test_migrate_dir_missing_source_is_empty_report(tmp_path: Path) -> None:
    memory_home = tmp_path / "MEMORY_HOME"
    (memory_home / "topics").mkdir(parents=True)
    report = migrate_dir(
        source_dir=tmp_path / "does-not-exist",
        memory_home=memory_home,
        dry_run=False,
        allow_llm=False,
    )
    assert report.migrated == 0
    assert report.skipped_missing == 1


def test_migrate_dir_marks_retired_even_when_all_files_fail(tmp_path: Path) -> None:
    """Stuck-state regression: when every file in source_dir fails classification,
    the migration marker must still be written so subsequent runs skip cleanly
    instead of looping forever on the same broken corpus.
    """
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)

    src = tmp_path / "broken-proj" / "memory"
    src.mkdir(parents=True)
    # No frontmatter at all → heuristic_classify returns None.  With allow_llm=False,
    # classify_file falls back to "knowledge" — that's not a failure mode that
    # populates report.errors.  To simulate genuine classification failure we need
    # to monkey-patch classify_file.  Easier: write a file that classify_file's
    # frontmatter loader cannot parse at all.
    (src / "x.md").write_text("---\n: : :\n: not valid yaml\n---\nbody\n")
    (src / "y.md").write_text("---\n@@@@\n---\nbody\n")

    from memory_tools.migrate import classify

    original = classify.classify_file

    def failing_classify(*args, **kwargs):
        raise RuntimeError("simulated classification failure")

    classify.classify_file = failing_classify
    # The migrator imports classify_file by name at module load — patch it there too.
    from memory_tools.migrate import migrator

    migrator.classify_file = failing_classify
    try:
        report = migrate_dir(
            source_dir=src, memory_home=memory_home, dry_run=False, allow_llm=False
        )
    finally:
        classify.classify_file = original
        migrator.classify_file = original

    assert report.migrated == 0
    assert len(report.errors) == 2
    # Marker must still be written so re-runs skip
    assert (src / MIGRATION_MARKER_NAME).is_file()


def test_migrate_dir_dedupes_byte_identical_bodies(tmp_path: Path) -> None:
    """Two source dirs holding the same legacy file under different project
    slugs must migrate the body once and skip the second copy.

    This is the regression test for the duplicate-pair bug, where the same
    repo accessed under two filesystem paths (e.g. ``~/Dropbox/github/X`` and
    ``~/Library/CloudStorage/Dropbox/github/X``) created two memory dirs and
    duplicated every file when migrated.
    """
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)
    (memory_home / "INDEX.md").write_text("# Index\n")

    body = "---\nname: foo\ntype: feedback\n---\n\nshared body content\n"
    src_a = tmp_path / "proj-a" / "memory"
    src_b = tmp_path / "proj-b" / "memory"
    src_a.mkdir(parents=True)
    src_b.mkdir(parents=True)
    (src_a / "feedback_x.md").write_text(body)
    (src_b / "feedback_x.md").write_text(body)

    rep_a = migrate_dir(source_dir=src_a, memory_home=memory_home, dry_run=False, allow_llm=False)
    rep_b = migrate_dir(source_dir=src_b, memory_home=memory_home, dry_run=False, allow_llm=False)

    assert rep_a.migrated == 1 and rep_a.skipped_duplicate == 0
    assert rep_b.migrated == 0 and rep_b.skipped_duplicate == 1
    # Only one when/ file ends up in MEMORY_HOME despite two sources
    when_files = sorted((memory_home / "when").iterdir())
    assert len(when_files) == 1, [f.name for f in when_files]
    # Both originals are archived (rescue path preserved)
    assert (src_a / "old-memory-archive" / "feedback_x.md").is_file()
    assert (src_b / "old-memory-archive" / "feedback_x.md").is_file()
