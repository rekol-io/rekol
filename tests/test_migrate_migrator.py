"""End-to-end tests for the migrator orchestrator."""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from memory_tools.migrate.migrator import (
    MigrationReport,
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
    # Retirement pointer
    memory_md = (src_parent / "memory" / "MEMORY.md").read_text()
    assert "RETIRED" in memory_md
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
    # MEMORY.md untouched (still original)
    assert "RETIRED" not in (src_parent / "memory" / "MEMORY.md").read_text()


def test_migrate_dir_idempotent_skips_retired(tmp_path: Path) -> None:
    src_parent, memory_home = _setup_fixture(tmp_path)
    migrate_dir(source_dir=src_parent / "memory", memory_home=memory_home,
                dry_run=False, allow_llm=False)
    # Second run — should be a no-op (pointer already set, no files to migrate)
    report = migrate_dir(source_dir=src_parent / "memory", memory_home=memory_home,
                          dry_run=False, allow_llm=False)
    assert report.migrated == 0
    assert report.skipped_retired == 1


def test_migrate_dir_collision_appends_suffix(tmp_path: Path) -> None:
    src_parent, memory_home = _setup_fixture(tmp_path)
    # Pre-create a target file that would collide
    (memory_home / "topics" / "proj-alpha.md").write_text("pre-existing")
    report = migrate_dir(source_dir=src_parent / "memory", memory_home=memory_home,
                          dry_run=False, allow_llm=False)
    assert report.migrated == 2
    # Migrated file landed at proj-alpha-2.md (suffix-1 reserved for actual collision)
    collided = (memory_home / "topics" / "proj-alpha-2.md")
    assert collided.is_file()
    # Original pre-existing file untouched
    assert (memory_home / "topics" / "proj-alpha.md").read_text() == "pre-existing"


def test_migrate_dir_writes_frontmatter_and_body(tmp_path: Path) -> None:
    src_parent, memory_home = _setup_fixture(tmp_path)
    migrate_dir(source_dir=src_parent / "memory", memory_home=memory_home,
                dry_run=False, allow_llm=False)
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
