"""The ``migrate repo`` contract, and the four gaps QA found in #227.

Every test here maps to something that was measured rather than imagined:

* **G1** dry-run previewed the ``MEMORY.md`` move in only one shape, because
  ``progressed`` keyed on ``migrated > 0`` and ``migrated`` stays 0 in dry-run.
  The shape that worked takes a different early return with its own preview, so
  the existing test passed while the ordinary path previewed nothing.
* **G2** a directory whose only ``.md`` files are nested retired in silence,
  because the warning sat below the ``if not files:`` return.
* **G3** the warning's remedy did nothing when followed literally: retired
  directories are filtered out of discovery, so re-running never looks again.
* **G4** an unwritable archive raised an uncaught ``PermissionError`` *after*
  the marker had been written, so the directory reported itself migrated and
  never retried.
* **contract** ``MEMORY.md`` outside Claude Code's projects tree is likely a
  tracked project file, so it must be left alone.

There is a test per state rather than one per feature on purpose: the states
here differ by which file exists, and a single test would pass while the
neighbouring state regressed — the #157/#158 pattern.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from rekol.migrate.archive import (
    ARCHIVE_DIR_NAME,
    MIGRATION_MARKER_NAME,
    write_retirement_pointer,
)
from rekol.migrate.discover import is_auto_memory_dir
from rekol.migrate.migrator import migrate_dir

TOPIC_FILE_BODY = """---
name: sailing
description: a topic
type: topics
---

Boats are good.
"""


def _memory_home(tmp_path: Path) -> Path:
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)
    return memory_home


def _source_dir(tmp_path: Path, *, with_memory_md: bool, with_topic: bool) -> Path:
    source_dir = tmp_path / "proj" / "memory"
    source_dir.mkdir(parents=True)
    if with_memory_md:
        (source_dir / "MEMORY.md").write_text("# My own index\n\nReal content.\n")
    if with_topic:
        (source_dir / "sailing.md").write_text(TOPIC_FILE_BODY)
    return source_dir


# --------------------------------------------------------------------------
# G1 — dry-run previews the move in EVERY shape, not just one
# --------------------------------------------------------------------------


def test_dry_run_previews_the_move_with_a_topic_file_alongside(tmp_path: Path) -> None:
    """The commonest shape of all, and the one that previewed nothing."""
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=True,
        allow_llm=False,
    )

    assert report.would_migrate == 1
    assert report.legacy_pointer_archived == source_dir / ARCHIVE_DIR_NAME / "MEMORY.md"
    # Dry-run must still touch nothing.
    assert (source_dir / "MEMORY.md").is_file()
    assert not (source_dir / MIGRATION_MARKER_NAME).exists()


def test_dry_run_previews_the_move_with_no_topic_file(tmp_path: Path) -> None:
    """The shape that already worked — kept so fixing one cannot break the other."""
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=False)

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=True,
        allow_llm=False,
    )

    assert report.legacy_pointer_archived == source_dir / ARCHIVE_DIR_NAME / "MEMORY.md"
    assert (source_dir / "MEMORY.md").is_file()


def test_dry_run_and_commit_agree_about_where_memory_md_goes(tmp_path: Path) -> None:
    """A preview that does not match what --commit does is worse than none."""
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)
    memory_home = _memory_home(tmp_path)

    previewed = migrate_dir(
        source_dir=source_dir,
        memory_home=memory_home,
        dry_run=True,
        allow_llm=False,
    ).legacy_pointer_archived
    committed = migrate_dir(
        source_dir=source_dir,
        memory_home=memory_home,
        dry_run=False,
        allow_llm=False,
    ).legacy_pointer_archived

    assert previewed == committed
    assert committed is not None and committed.is_file()


# --------------------------------------------------------------------------
# G2 — the nested warning fires in every nested shape
# --------------------------------------------------------------------------


def _nested_warning(report_warnings: list[str]) -> str | None:
    return next((w for w in report_warnings if "do not recurse" in w or "not recurse" in w), None)


@pytest.mark.parametrize(
    ("with_memory_md", "with_topic", "label"),
    [
        (False, False, "nested files only"),
        (True, False, "MEMORY.md plus a nested file"),
        (True, True, "MEMORY.md, a topic file and a nested file"),
        (False, True, "a topic file and a nested file"),
    ],
)
def test_nested_files_are_warned_about_in_every_shape(
    tmp_path: Path, with_memory_md: bool, with_topic: bool, label: str
) -> None:
    """The first two shapes retired in total silence: `migrated 0`, exit 0."""
    source_dir = _source_dir(tmp_path, with_memory_md=with_memory_md, with_topic=with_topic)
    (source_dir / "notes").mkdir()
    (source_dir / "notes" / "deep.md").write_text("nested content\n")

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
    )

    warning = _nested_warning(report.warnings)
    assert warning is not None, f"no nested-file warning for: {label}"
    assert "notes/deep.md" in warning
    # The file itself is never touched.
    assert (source_dir / "notes" / "deep.md").read_text() == "nested content\n"


def test_nested_files_are_warned_about_in_dry_run_too(tmp_path: Path) -> None:
    """Dry-run never mentioned them, for any shape (same cause as G1)."""
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)
    (source_dir / "notes").mkdir()
    (source_dir / "notes" / "deep.md").write_text("nested content\n")

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=True,
        allow_llm=False,
    )

    assert _nested_warning(report.warnings) is not None


def test_the_dry_run_warning_does_not_claim_the_directory_was_retired(tmp_path: Path) -> None:
    """A dry run retires nothing, so the past tense would be a false claim.

    Caught by running the real CLI, not by the tests above: they asserted the
    warning was PRESENT in dry-run and never read what it said. The wording was
    copied straight from the commit path and announced "This directory is now
    retired" after a run that had changed nothing — the same species of untrue
    statement this warning exists to prevent.
    """
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)
    (source_dir / "notes").mkdir()
    (source_dir / "notes" / "deep.md").write_text("nested content\n")

    dry = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=True,
        allow_llm=False,
    )
    warning = _nested_warning(dry.warnings)
    assert warning is not None
    assert "is now retired" not in warning, warning
    assert "Nothing has been retired yet" in warning
    # And the directory really was not retired, so the statement is true.
    assert not (source_dir / MIGRATION_MARKER_NAME).exists()

    committed = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
    )
    committed_warning = _nested_warning(committed.warnings)
    assert committed_warning is not None
    assert "is now retired" in committed_warning
    assert (source_dir / MIGRATION_MARKER_NAME).is_file()


def test_the_archive_dir_is_not_reported_as_a_nested_file(tmp_path: Path) -> None:
    """Negative control: archived originals live in a subdir and are not lost."""
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
    )

    assert (source_dir / ARCHIVE_DIR_NAME).is_dir()
    assert _nested_warning(report.warnings) is None


# --------------------------------------------------------------------------
# G3 — the remedy in the warning actually works when followed
# --------------------------------------------------------------------------


def test_the_nested_warning_names_the_marker_that_must_be_removed(tmp_path: Path) -> None:
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)
    (source_dir / "notes").mkdir()
    (source_dir / "notes" / "deep.md").write_text("nested content\n")

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
    )

    warning = _nested_warning(report.warnings)
    assert warning is not None
    # "Move them up one level and re-run" alone is what QA followed literally,
    # to no effect: retired dirs are filtered out of discovery.
    assert MIGRATION_MARKER_NAME in warning
    assert "NOT searchable" in warning


def test_following_the_remedy_actually_migrates_the_nested_file(tmp_path: Path) -> None:
    """The regression test for a documented remedy, executed rather than read."""
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)
    memory_home = _memory_home(tmp_path)
    (source_dir / "notes").mkdir()
    # A DISTINCT body: dedup is by body hash, so reusing the topic text here
    # would make this pass as `skipped_duplicate` and prove nothing.
    (source_dir / "notes" / "deep.md").write_text(
        TOPIC_FILE_BODY.replace("sailing", "diving").replace(
            "Boats are good.", "Wrecks are better."
        )
    )

    first = migrate_dir(
        source_dir=source_dir, memory_home=memory_home, dry_run=False, allow_llm=False
    )
    assert _nested_warning(first.warnings) is not None

    # Do exactly what the warning says: move it up, delete the marker, re-run.
    (source_dir / "notes" / "deep.md").rename(source_dir / "deep.md")
    (source_dir / MIGRATION_MARKER_NAME).unlink()

    second = migrate_dir(
        source_dir=source_dir, memory_home=memory_home, dry_run=False, allow_llm=False
    )

    assert second.migrated == 1, "the remedy did not migrate the file it promised to"
    assert second.skipped_retired == 0


# --------------------------------------------------------------------------
# G4 — an unwritable archive: no traceback, and NOT retired
# --------------------------------------------------------------------------


def _make_unwritable(path: Path) -> None:
    path.chmod(stat.S_IRUSR | stat.S_IXUSR)


@pytest.fixture
def _restore_permissions() -> object:
    created: list[Path] = []
    yield created
    for path in created:
        if path.exists():
            path.chmod(stat.S_IRWXU)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits this test relies on")
def test_an_unwritable_archive_does_not_retire_the_directory(
    tmp_path: Path, _restore_permissions: list[Path]
) -> None:
    """QA measured: traceback, exit 1, `.MIGRATED=yes`, MEMORY.md still there.

    The marker was the problem, not the failure — it made the strand permanent,
    because `has_migration_marker` then skips the directory for good.
    """
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("content that must survive\n")
    archive_dir = memory_dir / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    _restore_permissions.append(archive_dir)
    _make_unwritable(archive_dir)

    outcome = write_retirement_pointer(memory_dir, memory_home=tmp_path / "home")

    assert outcome.retired is False
    assert outcome.error is not None
    assert "could not archive" in outcome.error
    # The two states that make it recoverable:
    assert (memory_dir / "MEMORY.md").read_text() == "content that must survive\n"
    assert not (memory_dir / MIGRATION_MARKER_NAME).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits this test relies on")
def test_an_unwritable_archive_surfaces_as_an_error_not_a_traceback(
    tmp_path: Path, _restore_permissions: list[Path]
) -> None:
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)
    archive_dir = source_dir / ARCHIVE_DIR_NAME
    archive_dir.mkdir()

    # Let the topic file archive first, then block the legacy pointer's move.
    report_first = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
    )
    assert report_first.migrated == 1

    # Second generation: a fresh MEMORY.md with the archive now unwritable.
    (source_dir / "MEMORY.md").write_text("second generation\n")
    (source_dir / MIGRATION_MARKER_NAME).unlink()
    _restore_permissions.append(archive_dir)
    _make_unwritable(archive_dir)

    outcome = write_retirement_pointer(source_dir, memory_home=_memory_home(tmp_path))

    assert outcome.error is not None
    assert outcome.retired is False
    assert (source_dir / "MEMORY.md").read_text() == "second generation\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the mode bits this test relies on")
def test_a_retry_succeeds_once_the_archive_is_writable_again(
    tmp_path: Path, _restore_permissions: list[Path]
) -> None:
    """The point of not writing the marker: the work is retried, not lost."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("survives\n")
    archive_dir = memory_dir / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    _restore_permissions.append(archive_dir)
    _make_unwritable(archive_dir)

    assert write_retirement_pointer(memory_dir, memory_home=tmp_path / "home").retired is False

    archive_dir.chmod(stat.S_IRWXU)
    outcome = write_retirement_pointer(memory_dir, memory_home=tmp_path / "home")

    assert outcome.retired is True
    assert outcome.error is None
    assert outcome.legacy_pointer_archived is not None
    assert outcome.legacy_pointer_archived.read_text() == "survives\n"
    assert (memory_dir / MIGRATION_MARKER_NAME).is_file()


def test_the_marker_is_written_last_so_a_failure_leaves_nothing_claiming_success(
    tmp_path: Path,
) -> None:
    """Order asserted directly, since the ordering IS the fix.

    The previous docstring argued for marker-first on the grounds that the
    reverse "would leave neither file on partial failure, causing a re-run to
    re-migrate already-archived content". That is false: discovery iterates
    files only and never recurses, so the archive cannot be re-discovered.
    """
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("body\n")

    outcome = write_retirement_pointer(memory_dir, memory_home=tmp_path / "home")

    archived = outcome.legacy_pointer_archived
    assert archived is not None
    marker = memory_dir / MIGRATION_MARKER_NAME
    assert marker.is_file()
    # The archived copy must not be newer than the marker: the move happened first.
    assert archived.stat().st_mtime <= marker.stat().st_mtime + 1


def test_the_marker_text_does_not_wrap_mid_clause(tmp_path: Path) -> None:
    """Cosmetic, from QA: it read `KEEP them unless you` / `have` / `checked`."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    write_retirement_pointer(memory_dir, memory_home=tmp_path / "home")

    lines = (memory_dir / MIGRATION_MARKER_NAME).read_text().splitlines()
    assert "have" not in lines, f"a line is just the word 'have': {lines}"
    for line in lines:
        assert len(line.split()) != 1 or not line.strip(), f"single-word line: {line!r}"


# --------------------------------------------------------------------------
# The contract: MEMORY.md outside the projects tree is left alone
# --------------------------------------------------------------------------


def test_a_repo_memory_md_is_left_untouched(tmp_path: Path) -> None:
    """rekol ships a MEMORY.md in its own template; moving a tracked file is wrong."""
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)
    original = (source_dir / "MEMORY.md").read_text()

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
        archive_legacy_pointer=False,
    )

    assert (source_dir / "MEMORY.md").read_text() == original
    assert report.legacy_pointer_archived is None
    assert report.legacy_pointer_left_in_place == source_dir / "MEMORY.md"
    assert not (source_dir / ARCHIVE_DIR_NAME / "MEMORY.md").exists()
    # It is still retired: refusing would make a re-run retry forever.
    assert (source_dir / MIGRATION_MARKER_NAME).is_file()


def test_a_repo_memory_md_is_left_untouched_when_it_is_the_only_file(tmp_path: Path) -> None:
    """The early-return branch needs the contract too, not just the main path."""
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=False)
    original = (source_dir / "MEMORY.md").read_text()

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
        archive_legacy_pointer=False,
    )

    assert (source_dir / "MEMORY.md").read_text() == original
    assert report.legacy_pointer_left_in_place == source_dir / "MEMORY.md"


def test_dry_run_does_not_promise_to_move_a_repo_memory_md(tmp_path: Path) -> None:
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=True,
        allow_llm=False,
        archive_legacy_pointer=False,
    )

    assert report.legacy_pointer_archived is None
    assert report.legacy_pointer_left_in_place == source_dir / "MEMORY.md"


def test_an_auto_memory_dir_still_archives_it(tmp_path: Path) -> None:
    """The other half of the contract — the default must not have changed."""
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
    )

    assert report.legacy_pointer_left_in_place is None
    assert report.legacy_pointer_archived is not None
    assert report.legacy_pointer_archived.is_file()
    assert not (source_dir / "MEMORY.md").exists()


# --------------------------------------------------------------------------
# is_auto_memory_dir — what decides which half of the contract applies
# --------------------------------------------------------------------------


def test_is_auto_memory_dir_recognises_the_projects_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    memory_dir = tmp_path / "claude" / "projects" / "slug" / "memory"
    memory_dir.mkdir(parents=True)
    assert is_auto_memory_dir(memory_dir) is True


@pytest.mark.parametrize(
    "relative",
    [
        "somewhere/memory",
        "claude/projects/memory",
        "claude/projects/slug/nested/memory",
        "claude/projects/slug/notmemory",
    ],
)
def test_is_auto_memory_dir_rejects_everything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    memory_dir = tmp_path / relative
    memory_dir.mkdir(parents=True)
    assert is_auto_memory_dir(memory_dir) is False


def test_is_auto_memory_dir_honours_a_relocated_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#165's lesson: a relocated tree is still the tree autoMemory injects from."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    memory_dir = tmp_path / "elsewhere" / "projects" / "slug" / "memory"
    memory_dir.mkdir(parents=True)
    assert is_auto_memory_dir(memory_dir) is True
    # And the default location is no longer special once it is relocated.
    default_shape = tmp_path / "claude" / "projects" / "slug" / "memory"
    default_shape.mkdir(parents=True)
    assert is_auto_memory_dir(default_shape) is False
