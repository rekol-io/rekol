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

import contextlib
import errno
import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from rekol.cli_migrate import main
from rekol.migrate import archive as archive_module
from rekol.migrate import migrator as migrator_module
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


def test_the_marker_is_written_after_the_archive_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Order asserted DIRECTLY, by observing the calls.

    The first version of this test compared mtimes — which proves nothing,
    because a move preserves the original's mtime, so marker-first passed it too
    (Codex, 2026-11-06). A test that cannot fail on the defect it names is worse
    than no test: it reads as coverage.

    The order is the fix: archiving first means a failure leaves nothing claiming
    the directory is done. The reverse shipped briefly, and its stated rationale
    was also false — `discover_files_in_dir` never recurses, so the archive
    cannot be re-discovered.
    """
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text("body\n")

    calls: list[str] = []
    real_publish = archive_module.publish_into_archive
    real_marker = archive_module.write_migration_marker

    def observed_publish(source_path: Path, archive_dir: Path) -> Path:
        calls.append("archive")
        return real_publish(source_path, archive_dir)

    def observed_marker(marker: Path, text: str) -> None:
        calls.append("marker")
        real_marker(marker, text)

    monkeypatch.setattr(archive_module, "publish_into_archive", observed_publish)
    monkeypatch.setattr(archive_module, "write_migration_marker", observed_marker)

    outcome = archive_module.write_retirement_pointer(memory_dir, memory_home=tmp_path / "home")

    assert outcome.retired is True
    assert calls == ["archive", "marker"], f"wrong order: {calls}"


def test_a_partially_written_marker_never_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`has_migration_marker` only asks whether the file EXISTS.

    So a short write would be accepted as a completed migration while the report
    said the directory was not retired — the next run believes the file, not the
    report (Codex, 2026-11-06).
    """
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    marker = memory_dir / MIGRATION_MARKER_NAME

    real_replace = archive_module.os.replace

    def failing_replace(src, dst):  # noqa: ANN001, ARG001
        raise OSError("disk full")

    monkeypatch.setattr(archive_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        archive_module.write_migration_marker(marker, "migrated\n")

    # Before creation: no marker at all, and no debris under a real name.
    assert not marker.exists()
    assert [p.name for p in memory_dir.iterdir()] == []

    monkeypatch.setattr(archive_module.os, "replace", real_replace)
    archive_module.write_migration_marker(marker, "migrated\n")
    # After creation: complete, and nothing staged left behind.
    assert marker.read_text() == "migrated\n"
    assert [p.name for p in memory_dir.iterdir()] == [MIGRATION_MARKER_NAME]


def test_dry_run_predicts_the_suffixed_name_commit_would_choose(tmp_path: Path) -> None:
    """The preview promised MEMORY.md while --commit would pick MEMORY-1.md."""
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)
    archive_dir = source_dir / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    (archive_dir / "MEMORY.md").write_text("an earlier archived original\n")

    previewed = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=True,
        allow_llm=False,
    ).legacy_pointer_archived
    committed = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
    ).legacy_pointer_archived

    assert previewed == archive_dir / "MEMORY-1.md"
    assert previewed == committed, "the preview promised a different path"
    assert (archive_dir / "MEMORY.md").read_text() == "an earlier archived original\n"


def test_the_nested_warning_does_not_claim_retirement_when_it_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two contradictory statements in one report, with no way to tell which is true.

    Only the LEGACY POINTER's publication is made to fail. An unwritable archive
    directory cannot express this case: it fails the topic file's archiving too,
    so the directory never becomes retirable and the warning is never reached —
    which is how an earlier version of this test passed while asserting nothing.
    """
    source_dir = _source_dir(tmp_path, with_memory_md=True, with_topic=True)
    (source_dir / "notes").mkdir()
    (source_dir / "notes" / "deep.md").write_text("nested\n")

    real_publish = archive_module.publish_into_archive

    def publish_except_memory_md(source_path: Path, archive_dir: Path, **kwargs: object) -> Path:
        if source_path.name == "MEMORY.md":
            raise archive_module.ArchivePublishError(
                f"could not archive {source_path}: simulated. Nothing was moved."
            )
        return real_publish(source_path, archive_dir, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(archive_module, "publish_into_archive", publish_except_memory_md)

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
    )

    assert report.migrated == 1, "the topic file must import, or nothing is retirable"
    assert report.errors, "expected the archive failure to be reported"
    warning = _nested_warning(report.warnings)
    assert warning is not None, "the nested warning must still be raised"
    assert "is now retired" not in warning, warning
    assert "was NOT retired" in warning
    # And the claim is true: no marker, and MEMORY.md still in place.
    assert not (source_dir / MIGRATION_MARKER_NAME).exists()
    assert (source_dir / "MEMORY.md").is_file()


def test_an_imported_file_gets_a_suffix_when_the_name_is_taken(tmp_path: Path) -> None:
    """The ordinary sequential collision.

    NOT a regression test for the race: verified by reverting to
    `exists()` + `write_text`, where this still passes, because the old suffix
    loop handled the sequential case correctly. The race is covered by the test
    below, which injects a competitor between name selection and creation.
    """
    memory_home = _memory_home(tmp_path)
    # The real target name, confirmed by running the migrator: the project slug
    # is prefixed and an unclassified file defaults to knowledge/. Guessing
    # `topics/sailing.md` made an earlier version of this test pass without ever
    # creating a collision.
    existing = memory_home / "knowledge" / "proj-sailing.md"
    existing.write_text("ALREADY IMPORTED, must survive\n")

    source_dir = _source_dir(tmp_path, with_memory_md=False, with_topic=True)
    report = migrate_dir(
        source_dir=source_dir,
        memory_home=memory_home,
        dry_run=False,
        allow_llm=False,
    )

    assert report.migrated == 1
    assert existing.read_text() == "ALREADY IMPORTED, must survive\n"
    assert (memory_home / "knowledge" / "proj-sailing-1.md").is_file()


def test_a_competitor_creating_the_target_name_does_not_lose_the_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual race: a file appears at the chosen name before the write.

    `exists()` then `write_text` overwrote it. `O_CREAT|O_EXCL` is one atomic
    operation, so the loser of the race takes the next name instead of
    destroying the winner. Injected at `os.open`, which is the only place the
    name is claimed — the same probe shape Codex used for the archive.
    """
    memory_home = _memory_home(tmp_path)
    source_dir = _source_dir(tmp_path, with_memory_md=False, with_topic=True)
    contested = memory_home / "knowledge" / "proj-sailing.md"

    # Injected at os.link, which is where the name is now claimed: the import is
    # staged first and published by the same primitive the archive uses.
    real_link = archive_module.os.link
    raced = {"done": False}

    def racing_link(src, dst, **kwargs):  # noqa: ANN001
        if not raced["done"] and str(dst) == str(contested):
            raced["done"] = True
            # A competitor wins the name in the instant before our claim.
            contested.write_text("COMPETITOR'S IMPORT\n")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(archive_module.os, "link", racing_link)

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=memory_home,
        dry_run=False,
        allow_llm=False,
    )

    assert raced["done"], "the race was never triggered; this test proved nothing"
    assert report.migrated == 1
    # The competitor's content must survive untouched.
    assert contested.read_text() == "COMPETITOR'S IMPORT\n"
    assert (memory_home / "knowledge" / "proj-sailing-1.md").is_file()


def test_an_imported_file_is_not_written_through_a_dangling_symlink(tmp_path: Path) -> None:
    """`exists()` follows the link and returns False, so the name read as free.

    `write_text` then followed it and wrote THROUGH the link, creating or
    truncating whatever it pointed at — anywhere on disk.
    """
    memory_home = _memory_home(tmp_path)
    victim = tmp_path / "somewhere-else.md"
    (memory_home / "knowledge" / "proj-sailing.md").symlink_to(victim)

    source_dir = _source_dir(tmp_path, with_memory_md=False, with_topic=True)
    report = migrate_dir(
        source_dir=source_dir,
        memory_home=memory_home,
        dry_run=False,
        allow_llm=False,
    )

    assert report.migrated == 1
    # Nothing was written through the link.
    assert not victim.exists(), "wrote through a dangling symlink to an unrelated path"
    assert (memory_home / "knowledge" / "proj-sailing-1.md").is_file()


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


# --------------------------------------------------------------------------
# A git work-tree root: --commit refuses rather than moving tracked files
# --------------------------------------------------------------------------
# QA pointed `migrate repo` at a real repo and --commit moved the tracked
# README.md and CLAUDE.md into the archive, leaving them deleted in the work
# tree (`D CLAUDE.md`, `D README.md`, plus an untracked old-memory-archive/).
# Losing CLAUDE.md also takes a project's instructions out of Claude Code's
# view. #234's own reasoning for sparing MEMORY.md applies verbatim to its
# siblings, so the contract could not be stated truthfully while this stood.
#
# Refused, not forbidden: a git-tracked notes repo is a legitimate source, and
# rekol's own git_track makes REKOL_HOME a work-tree root.


def _git_repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "myrepo"
    (root / ".git").mkdir(parents=True)
    (root / "README.md").write_text("# project readme\n")
    (root / "CLAUDE.md").write_text("# project instructions for Claude\n")
    (root / "MEMORY.md").write_text("# index\n")
    return root


def _run_repo(args: list[str], memory_home: Path) -> object:
    runner = CliRunner()
    return runner.invoke(main, ["repo", *args], env={"MEMORY_HOME": str(memory_home)})


def test_commit_refuses_at_a_git_work_tree_root(tmp_path: Path) -> None:
    root = _git_repo_root(tmp_path)
    memory_home = _memory_home(tmp_path)

    result = _run_repo([str(root), "--commit", "--no-llm"], memory_home)

    assert result.exit_code == 2, result.output
    assert "git work-tree root" in result.output
    # The remedy has to be the one that works: point at the memory subdir.
    assert "memory" in result.output
    # Nothing was touched.
    assert (root / "README.md").read_text() == "# project readme\n"
    assert (root / "CLAUDE.md").read_text() == "# project instructions for Claude\n"
    assert not (root / ARCHIVE_DIR_NAME).exists()
    assert not (root / MIGRATION_MARKER_NAME).exists()


def test_dry_run_at_a_git_root_is_allowed_and_names_every_file(tmp_path: Path) -> None:
    """Dry-run must never be blocked: it is how you find out what would happen.

    And it has to NAME the files — QA's preview showed only
    `would migrate 2 (defaulted=2)`, so previewing could not reveal that
    README.md and CLAUDE.md were about to be moved.
    """
    root = _git_repo_root(tmp_path)
    memory_home = _memory_home(tmp_path)

    result = _run_repo([str(root), "--dry-run", "--no-llm"], memory_home)

    assert result.exit_code == 0, result.output
    assert "README.md" in result.output
    assert "CLAUDE.md" in result.output
    assert "WOULD move" in result.output
    # Still a dry run.
    assert (root / "README.md").is_file()
    assert not (root / ARCHIVE_DIR_NAME).exists()


def test_allow_repo_root_opts_in(tmp_path: Path) -> None:
    """The escape hatch for a genuine notes repo."""
    root = _git_repo_root(tmp_path)
    memory_home = _memory_home(tmp_path)

    result = _run_repo([str(root), "--commit", "--no-llm", "--allow-repo-root"], memory_home)

    assert result.exit_code == 0, result.output
    assert (root / ARCHIVE_DIR_NAME).is_dir()


def test_a_memory_subdir_inside_a_git_repo_is_not_refused(tmp_path: Path) -> None:
    """The ordinary shape: the repo is git, the memory dir inside it is not a root."""
    root = _git_repo_root(tmp_path)
    (root / "memory").mkdir()
    (root / "memory" / "sailing.md").write_text(TOPIC_FILE_BODY)
    memory_home = _memory_home(tmp_path)

    result = _run_repo([str(root), "--commit", "--no-llm"], memory_home)

    assert result.exit_code == 0, result.output
    # The repo's own tracked files are untouched because `memory/` was preferred.
    assert (root / "README.md").is_file()
    assert (root / "CLAUDE.md").is_file()
    assert (root / "memory" / ARCHIVE_DIR_NAME).is_dir()


def test_a_plain_directory_is_not_refused(tmp_path: Path) -> None:
    """Negative control: refusing every directory would pass the tests above."""
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "sailing.md").write_text(TOPIC_FILE_BODY)
    memory_home = _memory_home(tmp_path)

    result = _run_repo([str(plain), "--commit", "--no-llm"], memory_home)

    assert result.exit_code == 0, result.output
    assert (plain / ARCHIVE_DIR_NAME).is_dir()


def test_a_git_file_not_a_dir_is_also_a_work_tree_root(tmp_path: Path) -> None:
    """A worktree or submodule has `.git` as a FILE, and is just as destructive."""
    root = tmp_path / "wt"
    root.mkdir()
    (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    (root / "README.md").write_text("# readme\n")
    memory_home = _memory_home(tmp_path)

    result = _run_repo([str(root), "--commit", "--no-llm"], memory_home)

    assert result.exit_code == 2, result.output
    assert (root / "README.md").is_file()


# --------------------------------------------------------------------------
# The moved-MEMORY.md line must say "not searchable" too (product's ruling)
# --------------------------------------------------------------------------


def test_the_moved_memory_md_line_says_not_searchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The left-alone line already said it; the moved line did not."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    source_dir = tmp_path / "claude" / "projects" / "slug" / "memory"
    source_dir.mkdir(parents=True)
    (source_dir / "MEMORY.md").write_text("# index\n")
    (source_dir / "sailing.md").write_text(TOPIC_FILE_BODY)
    memory_home = _memory_home(tmp_path)

    committed = _run_repo(
        [str(tmp_path / "claude" / "projects" / "slug"), "--commit", "--no-llm"], memory_home
    )
    assert "moved MEMORY.md" in committed.output
    assert "not searchable" in committed.output, committed.output


def test_the_would_be_moved_memory_md_line_says_not_searchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    source_dir = tmp_path / "claude" / "projects" / "slug" / "memory"
    source_dir.mkdir(parents=True)
    (source_dir / "MEMORY.md").write_text("# index\n")
    (source_dir / "sailing.md").write_text(TOPIC_FILE_BODY)
    memory_home = _memory_home(tmp_path)

    previewed = _run_repo(
        [str(tmp_path / "claude" / "projects" / "slug"), "--dry-run", "--no-llm"], memory_home
    )
    assert "WOULD be moved" in previewed.output
    assert "not searchable" in previewed.output, previewed.output


# --------------------------------------------------------------------------
# Archive publication: no clobber, ever (Codex P1/P2, 2026-11-06)
# --------------------------------------------------------------------------
# These use Codex's probe shapes rather than sequential suffix cases, because
# sequential tests cannot see the race: `exists()` then `shutil.move` overwrote
# a file created in between, and `os.rename` replaces its destination silently.
# The archive holds the only byte-complete original, so one such overwrite is one
# unrecoverable file.


def test_a_competitor_claiming_the_name_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex's race probe: a file appears at the chosen name before publication.

    Reproduced without a production seam by making name generation itself lose
    the race — the competitor is created as the first candidate is handed out,
    which is exactly the window `exists()`-then-move left open.
    """
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    source = tmp_path / "topic.md"
    source.write_text("NEW\n")

    real_names = archive_module._candidate_archive_names
    raced = {"done": False}

    def racing_names(directory: Path, name: str):
        for candidate in real_names(directory, name):
            if not raced["done"]:
                raced["done"] = True
                candidate.write_text("EARLIER ORIGINAL\n")
            yield candidate

    monkeypatch.setattr(archive_module, "_candidate_archive_names", racing_names)

    published = archive_module.publish_into_archive(source, archive_dir)

    # The competitor's only bytes must still be there.
    assert (archive_dir / "topic.md").read_text() == "EARLIER ORIGINAL\n"
    assert published != archive_dir / "topic.md"
    assert published.read_text() == "NEW\n"
    assert not source.exists()


def test_a_dangling_symlink_destination_counts_as_occupied(tmp_path: Path) -> None:
    """`exists()` is False for a dangling symlink, so it used to read as free."""
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    (archive_dir / "topic.md").symlink_to(tmp_path / "nowhere.md")
    source = tmp_path / "topic.md"
    source.write_text("real content\n")

    published = archive_module.publish_into_archive(source, archive_dir)

    assert published == archive_dir / "topic-1.md"
    assert published.read_text() == "real content\n"
    # The dangling link is still there, not replaced.
    assert (archive_dir / "topic.md").is_symlink()


def test_a_symlink_source_is_archived_as_real_content(tmp_path: Path) -> None:
    """Codex P2: moving the LINK re-resolved a relative target and broke it.

    Following it instead makes "kept byte-complete in the archive" true, and
    leaves the target file where it is — only the link is removed, which is all
    that is needed to stop autoMemory injecting it.
    """
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    target_file = tmp_path / "real.txt"
    target_file.write_text("the only copy\n")
    link = tmp_path / "MEMORY.md"
    link.symlink_to("real.txt")  # RELATIVE, the shape that broke

    published = archive_module.publish_into_archive(link, archive_dir)

    assert not published.is_symlink(), "archived a link, not content"
    assert published.read_text() == "the only copy\n"
    # The target survives untouched, and the link is gone from the inject path.
    assert target_file.read_text() == "the only copy\n"
    assert not link.exists() and not link.is_symlink()


def test_a_dangling_symlink_source_is_refused_and_nothing_is_touched(tmp_path: Path) -> None:
    """No content to preserve, so removing it could still lose a reference."""
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    link = tmp_path / "MEMORY.md"
    link.symlink_to(tmp_path / "gone.md")

    with pytest.raises(archive_module.ArchivePublishError) as caught:
        archive_module.publish_into_archive(link, archive_dir)

    assert "nothing was moved" in str(caught.value).lower()
    assert link.is_symlink()
    assert list(archive_dir.iterdir()) == []


def test_partial_debris_is_not_mistaken_for_an_archived_original(tmp_path: Path) -> None:
    """A failed cross-filesystem copy can leave debris; it must not occupy a name."""
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    (archive_dir / ".partial-abcdef.tmp").write_text("half a fi")
    source = tmp_path / "topic.md"
    source.write_text("whole file\n")

    published = archive_module.publish_into_archive(source, archive_dir)

    # Debris never takes a real name, so the first one is still free.
    assert published == archive_dir / "topic.md"
    assert published.read_text() == "whole file\n"


def test_a_short_copy_on_the_fallback_is_refused_rather_than_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incomplete copy must never appear under a real archive name.

    Forced onto the COPY path, because a regular file now publishes by hard
    link, where there is no copy to be short. Without forcing it this test would
    pass while exercising nothing — the shape of the two tests I already had to
    throw away today.
    """
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    source = tmp_path / "topic.md"
    source.write_text("the whole thing matters\n")

    def no_hard_links(src, dst, **kwargs):  # noqa: ANN001, ARG001
        raise OSError(errno.EOPNOTSUPP, "no hard links here")

    def truncating_copy(src, dst, length=0):  # noqa: ANN001, ARG001
        dst.write(src.read(4))

    monkeypatch.setattr(archive_module.os, "link", no_hard_links)
    monkeypatch.setattr(archive_module.shutil, "copyfileobj", truncating_copy)

    with pytest.raises(archive_module.ArchivePublishError) as caught:
        archive_module.publish_into_archive(source, archive_dir)

    # The source is what matters: it must still be there, whole.
    assert source.read_text() == "the whole thing matters\n"
    assert "KEPT" in str(caught.value) or "changed while" in str(caught.value)
    # The published copy MAY exist, holding the bytes as they were when copied —
    # verification now happens after publication, which is what closes the
    # whole-copy window on this path. What must never exist is a FRAGMENT under
    # a real name: the final name only ever receives bytes via os.replace.
    published = archive_dir / "topic.md"
    if published.exists():
        assert published.read_text() in ("the whole thing matters\n", ""), published.read_text()
    assert [p.name for p in archive_dir.iterdir() if p.name.startswith(".partial-")] == []


def test_sequential_same_name_originals_still_get_suffixes(tmp_path: Path) -> None:
    """The ordinary case, kept so the no-clobber rewrite did not lose it."""
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    published = []
    for body in ("first\n", "second\n", "third\n"):
        source = tmp_path / "topic.md"
        source.write_text(body)
        published.append(archive_module.publish_into_archive(source, archive_dir))

    assert [p.name for p in published] == ["topic.md", "topic-1.md", "topic-2.md"]
    assert [p.read_text() for p in published] == ["first\n", "second\n", "third\n"]


# --------------------------------------------------------------------------
# The data-loss window the no-clobber fix introduced (Codex P1, product ruling)
# --------------------------------------------------------------------------
# Copy, then unconditional unlink, loses an edit made in between: the source is
# gone and the archive holds the old bytes. The rename it replaced had no such
# window. So hard-link publication is the ordinary path — same inode, nothing to
# lose — and the copy path re-verifies the source before removing anything.


def test_a_regular_file_is_published_by_hard_link_with_no_copy(tmp_path: Path) -> None:
    """Same inode means there is no second copy to go stale, and no window."""
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    source = tmp_path / "topic.md"
    source.write_text("content\n")
    inode_before = source.stat().st_ino

    published = archive_module.publish_into_archive(source, archive_dir)

    assert published.stat().st_ino == inode_before, "published a copy, not the inode"
    assert published.read_text() == "content\n"
    assert not source.exists()


def test_a_same_length_edit_during_the_fallback_copy_keeps_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex's same-length probe: the edit a size check cannot see.

    On the copy path the source is re-read and re-hashed before anything is
    removed. A same-length edit is exactly the case that a size comparison would
    wave through, which is why the check is a digest.
    """
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    source = tmp_path / "topic.md"
    source.write_text("AAAAAAAA\n")

    def no_hard_links(src, dst, **kwargs):  # noqa: ANN001, ARG001
        raise OSError(errno.EOPNOTSUPP, "no hard links here")

    real_copyfileobj = archive_module.shutil.copyfileobj

    def copy_then_edit_source(src, dst, length=0):  # noqa: ANN001, ARG001
        real_copyfileobj(src, dst)
        # A concurrent writer lands an edit of IDENTICAL length.
        source.write_text("BBBBBBBB\n")

    monkeypatch.setattr(archive_module.os, "link", no_hard_links)
    monkeypatch.setattr(archive_module.shutil, "copyfileobj", copy_then_edit_source)

    with pytest.raises(archive_module.ArchivePublishError) as caught:
        archive_module.publish_into_archive(source, archive_dir)

    # The NEW bytes survive at the source — that is the loss this prevents.
    assert source.read_text() == "BBBBBBBB\n"
    assert "KEPT" in str(caught.value)
    # The archive holds the bytes as they were, which is not a loss: both
    # versions exist and the directory is not retired, so a re-run archives the
    # current content. What must never exist is a fragment.
    published = archive_dir / "topic.md"
    if published.exists():
        assert published.read_text() == "AAAAAAAA\n", published.read_text()
    assert [p.name for p in archive_dir.iterdir() if p.name.startswith(".partial-")] == []


# --------------------------------------------------------------------------
# Dangling and looping MEMORY.md, through the ENTRYPOINT (product: test there)
# --------------------------------------------------------------------------


def test_a_dangling_memory_md_does_not_retire_the_directory(tmp_path: Path) -> None:
    """It was retired with no errors, so the link's content was abandoned."""
    source_dir = _source_dir(tmp_path, with_memory_md=False, with_topic=True)
    (source_dir / "MEMORY.md").symlink_to(source_dir / "gone.md")

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
    )

    assert report.errors, "a dangling MEMORY.md must be reported"
    assert not (source_dir / MIGRATION_MARKER_NAME).exists(), "retired despite the failure"
    assert (source_dir / "MEMORY.md").is_symlink(), "the link itself was removed"


def test_a_self_looping_memory_md_is_reported_not_an_uncaught_eloop(tmp_path: Path) -> None:
    """A self-loop raised ELOOP out of the migrator; it is a plain error now."""
    source_dir = _source_dir(tmp_path, with_memory_md=False, with_topic=True)
    loop = source_dir / "MEMORY.md"
    loop.symlink_to(loop)

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=_memory_home(tmp_path),
        dry_run=False,
        allow_llm=False,
    )

    assert report.errors, "a looping MEMORY.md must be reported"
    assert any("symlink" in err for err in report.errors), report.errors
    assert not (source_dir / MIGRATION_MARKER_NAME).exists()
    assert loop.is_symlink()


# --------------------------------------------------------------------------
# A fault mid-import must leave NO file under an ordinary name (QA, product)
# --------------------------------------------------------------------------
# Reserving the final name with O_EXCL and then writing into it left a
# TRUNCATED memory under a real name when the write failed part-way. A full disk
# is an ordinary fault, and the result is not an absent memory but a WRONG one:
# the fragment parses, the indexer indexes it, and in QA's fixtures search
# ranked it ABOVE the whole copy. That is memory silently wrong on an ordinary
# path, which is why it holds the tag.


def _cut_after_frontmatter(body: str) -> str:
    """QA's shape: the frontmatter survives, so the fragment still parses."""
    marker = "---\n"
    second = body.find(marker, body.find(marker) + len(marker))
    return body[: second + len(marker)]


class _FaultingWriter:
    """Writes only as far as the frontmatter, then raises ``fault``."""

    def __init__(self, handle: object, fault: BaseException) -> None:
        self._handle = handle
        self._fault = fault

    def write(self, text: str) -> int:
        self._handle.write(_cut_after_frontmatter(text))
        self._handle.flush()
        raise self._fault

    def __getattr__(self, name: str) -> object:
        return getattr(self._handle, name)

    def __enter__(self) -> _FaultingWriter:
        return self

    def __exit__(self, *exc: object) -> bool:
        self._handle.close()
        return False


def _import_with_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: BaseException
) -> tuple[Path, Path, Path]:
    """Run one import whose write fails mid-body.

    Returns ``(memory_home, layer_dir, source_dir)``.
    """
    memory_home = _memory_home(tmp_path)
    source_dir = _source_dir(tmp_path, with_memory_md=False, with_topic=True)
    real_fdopen = migrator_module.os.fdopen

    def faulting_fdopen(fd, mode="r", *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        handle = real_fdopen(fd, mode, *args, **kwargs)
        if "w" in mode:
            return _FaultingWriter(handle, fault)
        return handle

    monkeypatch.setattr(migrator_module.os, "fdopen", faulting_fdopen)
    with contextlib.suppress(BaseException):
        migrate_dir(
            source_dir=source_dir,
            memory_home=memory_home,
            dry_run=False,
            allow_llm=False,
        )
    monkeypatch.undo()
    return memory_home, memory_home / "knowledge", source_dir


def _ordinary_md_files(layer: Path) -> list[Path]:
    return [p for p in layer.iterdir() if p.suffix == ".md" and not p.name.startswith(".")]


def test_a_full_disk_mid_import_leaves_no_truncated_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOSPC after the frontmatter: no ordinary name, and no staging debris."""
    import errno as errno_mod

    _memory, layer, _source = _import_with_fault(
        tmp_path, monkeypatch, OSError(errno_mod.ENOSPC, "No space left on device")
    )

    assert _ordinary_md_files(layer) == [], "a truncated memory was published"
    assert [p.name for p in layer.iterdir() if p.name.startswith(".partial-")] == [], (
        "staging debris left behind"
    )


def test_a_hard_kill_mid_import_leaves_no_truncated_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same for a KeyboardInterrupt, which is why the cleanup catches BaseException."""
    _memory, layer, _source = _import_with_fault(tmp_path, monkeypatch, KeyboardInterrupt())

    assert _ordinary_md_files(layer) == [], "a truncated memory was published"
    assert [p.name for p in layer.iterdir() if p.name.startswith(".partial-")] == []


def test_a_retry_after_a_full_disk_imports_exactly_one_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of leaving nothing: the retry is clean, not a duplicate or a fragment."""
    import errno as errno_mod

    memory_home, layer, source_dir = _import_with_fault(
        tmp_path, monkeypatch, OSError(errno_mod.ENOSPC, "No space left on device")
    )
    assert _ordinary_md_files(layer) == []
    # The source survives the failed run, so the retry has something to import.
    assert (source_dir / "sailing.md").is_file(), "the failed run consumed the source"

    report = migrate_dir(
        source_dir=source_dir,
        memory_home=memory_home,
        dry_run=False,
        allow_llm=False,
    )

    assert report.migrated == 1
    files = _ordinary_md_files(layer)
    assert len(files) == 1, f"expected exactly one whole file, got {[f.name for f in files]}"
    # Whole, not cut: the body survived, not just the frontmatter.
    assert "Boats are good." in files[0].read_text()


# --------------------------------------------------------------------------
# Codex's three P1 probes (2026-11-06)
# --------------------------------------------------------------------------


def test_an_atomic_save_replacing_the_source_does_not_lose_the_new_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1a: "same inode, no content window" holds only while the NAME denotes it.

    An editor's atomic save replaces the source *pathname* with a brand-new
    file. The unconditional unlink then deleted the NEW bytes while the archive
    kept the old ones — a loss path the accepted copy-path micro-window does not
    cover, because the hard-link path had no source-identity check at all.
    """
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    source = tmp_path / "topic.md"
    source.write_text("OLD\n")

    real_link = archive_module.os.link
    swapped = {"done": False}

    def link_then_atomic_save(src, dst, **kwargs):  # noqa: ANN001
        result = real_link(src, dst, **kwargs)
        if not swapped["done"]:
            swapped["done"] = True
            # Exactly an editor's save: write a new file and rename it over.
            replacement = tmp_path / "topic.md.new"
            replacement.write_text("NEW\n")
            os.replace(replacement, source)
        return result

    monkeypatch.setattr(archive_module.os, "link", link_then_atomic_save)

    with pytest.raises(archive_module.ArchivePublishError) as caught:
        archive_module.publish_into_archive(source, archive_dir)

    assert swapped["done"], "the probe never fired; this test proved nothing"
    # The new bytes must survive.
    assert source.read_text() == "NEW\n"
    assert "was replaced while it was being archived" in str(caught.value)
    # And the old bytes are still reachable, because the published link is the
    # only name they have left — removing it would have destroyed them.
    assert (archive_dir / "topic.md").read_text() == "OLD\n"


def test_a_source_mutation_during_publication_is_caught_after_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1b: verification must cover the publication step itself."""
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    source = tmp_path / "topic.md"
    source.write_text("AAAA\n")

    def no_hard_links(src, dst, **kwargs):  # noqa: ANN001, ARG001
        raise OSError(errno.EOPNOTSUPP, "no hard links here")

    real_replace = archive_module.os.replace
    mutated = {"done": False}

    def replace_then_mutate(src, dst):  # noqa: ANN001
        result = real_replace(src, dst)
        if not mutated["done"]:
            mutated["done"] = True
            source.write_text("BBBB\n")  # same length: a size check misses it
        return result

    monkeypatch.setattr(archive_module.os, "link", no_hard_links)
    monkeypatch.setattr(archive_module.os, "replace", replace_then_mutate)

    with pytest.raises(archive_module.ArchivePublishError) as caught:
        archive_module.publish_into_archive(source, archive_dir)

    assert mutated["done"], "the probe never fired; this test proved nothing"
    assert source.read_text() == "BBBB\n", "the new bytes were lost"
    assert "KEPT" in str(caught.value)


def test_an_interrupted_fallback_import_never_leaves_a_fragment_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1c: imports inherited the fallback that copied into the final name.

    A valid-frontmatter fragment left at `knowledge/<name>.md` is directly
    visible to the indexer's markdown walk — QA's wrong-memory hazard. With
    placeholder-then-``os.replace`` the final name can only ever hold an empty
    placeholder or the complete file, so the worst case is inert.
    """
    memory_home = _memory_home(tmp_path)
    source_dir = _source_dir(tmp_path, with_memory_md=False, with_topic=True)
    layer = memory_home / "knowledge"

    def no_hard_links(src, dst, **kwargs):  # noqa: ANN001, ARG001
        raise OSError(errno.EOPNOTSUPP, "no hard links here")

    def interrupt_at_replace(src, dst):  # noqa: ANN001, ARG001
        raise KeyboardInterrupt

    monkeypatch.setattr(archive_module.os, "link", no_hard_links)
    monkeypatch.setattr(archive_module.os, "replace", interrupt_at_replace)

    with contextlib.suppress(BaseException):
        migrate_dir(
            source_dir=source_dir,
            memory_home=memory_home,
            dry_run=False,
            allow_llm=False,
        )
    monkeypatch.undo()

    # Whatever is left under an ordinary .md name must not be a fragment: either
    # nothing, or the empty placeholder.
    for path in layer.iterdir():
        if path.suffix == ".md" and not path.name.startswith("."):
            assert path.stat().st_size == 0, (
                f"a fragment is visible as a memory: {path.name} -> {path.read_text()!r}"
            )

    # And an empty placeholder is inert, asserted through the INDEXER rather
    # than a validation helper — what matters is whether it can become a
    # searchable memory, which is the "check it's harmless" product asked for.
    placeholders = [p for p in layer.iterdir() if p.suffix == ".md" and p.stat().st_size == 0]
    if placeholders:
        from rekol.embeddings import HashingEmbedder
        from rekol.indexer import Indexer
        from rekol.store import IndexStore

        embedder = HashingEmbedder(dim=384)
        store = IndexStore(
            db_path=tmp_path / "index.db",
            dim=embedder.dim,
            use_sqlite_vec=False,
            embedding_model="test-hashing",
        )
        store.init_schema()
        stats = Indexer(
            memory_root=memory_home,
            store=store,
            embedder=embedder,
            index_dir=tmp_path / "idx",
        ).rebuild()
        store.close()

        assert stats.files_indexed == 0, f"an empty placeholder was indexed as a memory: {stats}"
        assert stats.chunks_written == 0


def test_an_atomic_save_during_the_final_hash_does_not_lose_the_new_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1 (batch 4): a digest is not evidence about a PATHNAME.

    Hashing reads through an open descriptor, so an editor that replaces the
    pathname mid-read leaves the descriptor returning the OLD inode's bytes to
    EOF. The comparison against the archived copy then succeeds and the unlink
    deletes the NEW file. Codex reproduced it with a 3 MiB source replaced after
    the first 1 MiB: publication reported success, the source was gone, the
    archive held the old content, and the new content existed nowhere.

    This is a WHOLE-HASH window, not the brief post-check interval product
    accepted, so it is checked rather than accepted.
    """
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    source = tmp_path / "topic.md"
    # Several read chunks' worth, so there is a mid-hash moment to intervene at.
    source.write_text("A" * (3 * 1024 * 1024))

    def no_hard_links(src, dst, **kwargs):  # noqa: ANN001, ARG001
        raise OSError(errno.EOPNOTSUPP, "no hard links here")

    real_read = archive_module.os.read
    state = {"reads": 0, "swapped": False}

    def read_then_swap(fd, length):  # noqa: ANN001
        chunk = real_read(fd, length)
        state["reads"] += 1
        # Only during the SOURCE hash, which is the second file hashed: the
        # staged copy is hashed first. Swapping on any read after the first
        # megabyte of a multi-chunk file is enough to land mid-hash.
        if state["reads"] > 4 and not state["swapped"]:
            state["swapped"] = True
            replacement = tmp_path / "editor-temp"
            replacement.write_text("the new content the user just saved\n")
            os.replace(replacement, source)
        return chunk

    monkeypatch.setattr(archive_module.os, "link", no_hard_links)
    monkeypatch.setattr(archive_module.os, "read", read_then_swap)

    with pytest.raises(archive_module.ArchivePublishError) as caught:
        archive_module.publish_into_archive(source, archive_dir)

    monkeypatch.undo()
    assert state["swapped"], "the probe never fired; this test proved nothing"
    assert source.exists(), "the source was unlinked"
    assert source.read_text() == "the new content the user just saved\n", (
        "the newly saved bytes were lost"
    )
    assert "KEPT" in str(caught.value), str(caught.value)
    assert "not retired" in str(caught.value), str(caught.value)


def test_an_in_place_write_during_the_final_hash_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: no replacement, the same inode written through.

    Deliberately NOT claiming which check catches this. I measured it: removing
    the before/after descriptor comparison leaves this test passing, because an
    in-place write changes both the content (caught by the digest comparison)
    and the mtime (caught by the pathname stat). What this test owns is the
    OUTCOME — the write is not lost and retirement is refused — which is the
    property worth pinning regardless of which check fires.
    """
    archive_dir = tmp_path / ARCHIVE_DIR_NAME
    archive_dir.mkdir()
    source = tmp_path / "topic.md"
    source.write_text("A" * (3 * 1024 * 1024))

    def no_hard_links(src, dst, **kwargs):  # noqa: ANN001, ARG001
        raise OSError(errno.EOPNOTSUPP, "no hard links here")

    real_read = archive_module.os.read
    state = {"reads": 0, "written": False}

    def read_then_write_in_place(fd, length):  # noqa: ANN001
        chunk = real_read(fd, length)
        state["reads"] += 1
        if state["reads"] > 4 and not state["written"]:
            state["written"] = True
            with open(source, "r+b") as handle:  # noqa: PTH123
                handle.seek(0)
                handle.write(b"ZZZZ")
        return chunk

    monkeypatch.setattr(archive_module.os, "link", no_hard_links)
    monkeypatch.setattr(archive_module.os, "read", read_then_write_in_place)

    with pytest.raises(archive_module.ArchivePublishError) as caught:
        archive_module.publish_into_archive(source, archive_dir)

    monkeypatch.undo()
    assert state["written"], "the probe never fired; this test proved nothing"
    assert source.exists(), "the source was unlinked"
    assert source.read_bytes()[:4] == b"ZZZZ", "the in-place write was lost"
    assert "KEPT" in str(caught.value), str(caught.value)


# ---------------------------------------------------------------------------
# THE IMPORT/RETIRE CONTRACT (QA + product, 2026-11-07)
# ---------------------------------------------------------------------------
# The home never holds a version other than the one retired; a refused
# retirement leaves no import from that file; a retry leaves ONE memory with the
# current content. QA's walnut/copper fixture, run on both publication paths
# because they fail for different reasons.
#
# THE ARCHIVE ENTRY IS A SEPARATE QUESTION, and product's ruling on it reversed
# on 2026-11-07 after Codex showed the cost: the entry is removed only when
# those same bytes keep another name, which is true of a hard link to the live
# source and never true of a verified copy. So the two parametrisations below
# legitimately expect different archive states, and that asymmetry is the
# contract rather than an oversight — "no archive entry either" was the previous
# round's wording and it licensed destroying the last copy of a file.

_OLD = "# Staleness\n\nOLD deploy target is walnut.\n"
_NEW = "# Staleness\n\nNEW deploy target is copper.\n"


def _edit_in_place(path: Path, text: str) -> None:
    """Rewrite through the SAME inode, which is what keeps the identity check happy."""
    with open(path, "w") as handle:  # noqa: PTH123
        handle.write(text)


def _home_bodies(home: Path) -> list[str]:
    return ["OLD" if "walnut" in p.read_text() else "NEW" for p in sorted(home.rglob("*.md"))]


def _archive_bodies(root: Path) -> list[str]:
    return [
        "OLD" if "walnut" in p.read_text() else "NEW"
        for p in sorted(root.rglob(f"{ARCHIVE_DIR_NAME}/*"))
        if p.is_file()
    ]


@pytest.mark.parametrize("hard_links", [True, False], ids=["hard-link", "no-hard-link"])
def test_an_edit_after_the_import_refuses_and_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hard_links: bool
) -> None:
    """MEASURED BEFORE THE FIX, on the ordinary hard-link path: the import read
    OLD, the user edited in place, the hard link published NEW, the source was
    unlinked and the directory retired — **exit 0**. The newest text existed
    only in the archive, which is not searchable, and the home held a stale
    memory with nothing to flag it. QA found the copy-path half; product asked
    for this one measured, and it reproduced.

    Identity is not content: an in-place write keeps the inode, so the check
    added for Codex's P1a passed.
    """
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (legacy / "p-note.md").write_text(_OLD)

    fired = {"edit": False}

    def edit_before_publishing(*args, **kwargs):  # noqa: ANN002, ANN003
        if not fired["edit"]:
            fired["edit"] = True
            _edit_in_place(legacy / "p-note.md", _NEW)
        if not hard_links:
            raise OSError(errno.EOPNOTSUPP, "no hard links here")
        return real_link(*args, **kwargs)

    real_link = archive_module.os.link
    monkeypatch.setattr(archive_module.os, "link", edit_before_publishing)
    report = migrator_module.migrate_dir(legacy, memory_home=home, dry_run=False, allow_llm=False)
    monkeypatch.undo()

    assert fired["edit"], "the probe never fired; this test proved nothing"
    assert report.migrated == 0, report.migrated
    assert report.errors, "the run reported nothing"
    message = report.errors[0]

    # 1. The home holds no version at all, rather than the stale one.
    assert _home_bodies(home) == [], _home_bodies(home)
    # 2. The archive entry goes only when those bytes keep another name.
    #    HARD LINK: the entry IS the source's inode, so removing the extra name
    #    cannot destroy anything, and leaving it would track the user's later
    #    edits from inside a directory called an archive.
    #    COPY: a distinct inode, so the entry is the only copy of the bytes as
    #    they were read. It stays, and the message says so. Untidy beats gone.
    if hard_links:
        assert _archive_bodies(tmp_path) == [], _archive_bodies(tmp_path)
        assert "was removed, which was safe" in message, message
    else:
        # NEW, not OLD: on this path the probe fires before the copy, so the
        # copy is of the edited source. The entry is therefore byte-identical to
        # the live source and removing it would provably lose nothing — but the
        # rule is deliberately one rule (same inode, another name) rather than a
        # second content proof in a function already on its fifth round. The
        # cost is an untidy archive, which nothing searches.
        assert _archive_bodies(tmp_path) == ["NEW"], _archive_bodies(tmp_path)
        assert "was KEPT: it is not a second name" in message, message
    # 3. The source survives carrying the user's current content.
    assert (legacy / "p-note.md").read_text() == _NEW
    # 4. The message is true about everything, not only the source.
    assert "was KEPT" in message and "not retired" in message, message
    assert "import" in message and "was removed" in message, message


@pytest.mark.parametrize("hard_links", [True, False], ids=["hard-link", "no-hard-link"])
def test_a_retry_after_a_refusal_leaves_one_memory_with_the_current_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hard_links: bool
) -> None:
    """QA measured the retry leaving `p-note.md` (OLD) beside `p-note-1.md` (NEW).

    Same name, same dates, nothing to tell them apart, and search returned the
    stale one first — both came back as equals. Nothing was lost, and it was
    silently wrong, which is the bar.
    """
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (legacy / "p-note.md").write_text(_OLD)

    fired = {"edit": False}
    real_link = archive_module.os.link

    def edit_before_publishing(*args, **kwargs):  # noqa: ANN002, ANN003
        if not fired["edit"]:
            fired["edit"] = True
            _edit_in_place(legacy / "p-note.md", _NEW)
        if not hard_links:
            raise OSError(errno.EOPNOTSUPP, "no hard links here")
        return real_link(*args, **kwargs)

    monkeypatch.setattr(archive_module.os, "link", edit_before_publishing)
    migrator_module.migrate_dir(legacy, memory_home=home, dry_run=False, allow_llm=False)
    monkeypatch.undo()
    assert fired["edit"], "the probe never fired; this test proved nothing"

    retry = migrator_module.migrate_dir(legacy, memory_home=home, dry_run=False, allow_llm=False)

    assert retry.errors == [], retry.errors
    assert retry.migrated == 1, retry.migrated
    # EXACTLY ONE memory, holding the current content. No `-1` twin. This is the
    # clause that matters: the home is what search reads.
    assert _home_bodies(home) == ["NEW"], _home_bodies(home)
    if hard_links:
        assert _archive_bodies(tmp_path) == ["NEW"], _archive_bodies(tmp_path)
    else:
        # The kept copy-path entry from the refused run is still there, so the
        # retry's entry lands beside it as `p-note-1.md`. This is the accepted
        # cost of not destroying bytes on a guess (product, 2026-11-07): an
        # untidy archive, which no one searches, rather than a lost version.
        assert _archive_bodies(tmp_path) == ["NEW", "NEW"], _archive_bodies(tmp_path)


def test_a_kill_between_the_import_and_the_archive_ends_whole_after_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash window the contract has to survive.

    A `SystemExit` from the archive step bypasses the error handling entirely,
    which is what a kill looks like: the import is on disk and the source is
    untouched. The retry must end with one memory, not a twin.
    """
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (legacy / "p-note.md").write_text(_NEW)

    def die(*args, **kwargs):  # noqa: ANN002, ANN003
        raise SystemExit("killed between the import and the archive")

    monkeypatch.setattr(migrator_module, "archive_file", die)
    with pytest.raises(SystemExit):
        migrator_module.migrate_dir(legacy, memory_home=home, dry_run=False, allow_llm=False)
    monkeypatch.undo()

    # The import landed; the source is still there. That is the crash state.
    assert _home_bodies(home) == ["NEW"], _home_bodies(home)
    assert (legacy / "p-note.md").exists()

    retry = migrator_module.migrate_dir(legacy, memory_home=home, dry_run=False, allow_llm=False)
    assert retry.errors == [], retry.errors
    assert _home_bodies(home) == ["NEW"], _home_bodies(home)
    assert _archive_bodies(tmp_path) == ["NEW"], _archive_bodies(tmp_path)


def test_a_failed_undo_says_the_import_is_still_there(tmp_path: Path) -> None:
    """Never claim a removal that did not happen (product, 2026-11-07).

    Two ways the undo declines: the file is no longer what this run wrote, so it
    is not ours to delete; or the unlink fails. Both have to name the file and
    say it is still there.
    """
    published = tmp_path / "knowledge" / "note.md"
    published.parent.mkdir(parents=True)
    published.write_text("something else edited it")

    message = migrator_module._undo_import(published, "what this run wrote")
    assert "NOT removed" in message, message
    assert str(published) in message, message
    assert "still there" in message, message
    assert published.exists(), "a file that was not ours was deleted anyway"

    # And the ordinary case does remove it, so the guard is not refusing always.
    published.write_text("what this run wrote")
    ok = migrator_module._undo_import(published, "what this run wrote")
    assert "was removed" in ok and "NOT removed" not in ok, ok
    assert not published.exists()


# ---------------------------------------------------------------------------
# BATCH 6: the four ways the refusal handling was still wrong (Codex + product,
# 2026-11-07). Each of these was reproduced against the previous head before
# being fixed, and each fails if its own fix is reverted.
# ---------------------------------------------------------------------------


def test_a_replacement_before_the_final_hash_keeps_the_only_copy_of_the_old_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1: the mismatch branch deleted the last link to the old bytes.

    The hard-link path's identity check passes, and THEN the source pathname is
    replaced — an editor's atomic save. The archive entry is now the only name
    for OLD, with a link count of 1. The final hash correctly sees NEW and
    refuses, but the cleanup used to unlink the archive entry on the strength of
    an argument ("it is a hard link to the still-present source") that had
    stopped being true one line earlier. The import undo then removed the only
    other copy of OLD, so OLD survived nowhere.

    The user's current content was never at risk — it is in the source, and they
    overwrote OLD themselves. What this test holds is the default: bytes are not
    removed on a guess.
    """
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    source = legacy / "p-note.md"
    source.write_text(_OLD)

    real_digest_of = archive_module._digest_of
    fired = {"replaced": False, "hashes": 0}

    def replace_then_hash(path: Path):  # noqa: ANN202
        # The SECOND hash of the source, which is the one immediately before the
        # unlink. The first is the migrator's pre-pass, and replacing the file
        # there would simply make NEW the expected digest and prove nothing.
        if path == source:
            fired["hashes"] += 1
        if fired["hashes"] == 2 and not fired["replaced"]:
            fired["replaced"] = True
            # A REPLACEMENT, not an in-place write: a brand-new inode takes the
            # name, so the archive entry is orphaned at link count 1.
            replacement = legacy / ".new"
            replacement.write_text(_NEW)
            os.replace(replacement, source)
        return real_digest_of(path)

    monkeypatch.setattr(archive_module, "_digest_of", replace_then_hash)
    report = migrate_dir(legacy, memory_home=home, dry_run=False, allow_llm=False)
    monkeypatch.undo()

    assert fired["replaced"], "the probe never fired; this test proved nothing"
    assert report.migrated == 0, report.migrated
    assert report.errors, "the run reported nothing"
    message = report.errors[0]

    # THE POINT: OLD still exists, in the archive, because nothing else named it.
    assert _archive_bodies(tmp_path) == ["OLD"], _archive_bodies(tmp_path)
    assert "was KEPT: it is not a second name" in message, message
    # The source holds the user's current content, and the home holds no stale
    # version of it.
    assert source.read_text() == _NEW
    assert _home_bodies(home) == [], _home_bodies(home)
    # Not retired, so a re-run picks up NEW.
    assert not (legacy / MIGRATION_MARKER_NAME).exists()


def test_a_file_whose_hash_fails_is_not_migrated_or_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1: verification was optional exactly where it was needed.

    A failed initial hash was logged and the run carried on with
    ``expected_digest=None``, which turned the whole import/retire check off for
    the one file nothing could vouch for. With an edit before publication that
    left OLD in the home and retired NEW. Fail closed instead: no import, no
    archive, source untouched, directory not retired.
    """
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    source = legacy / "p-note.md"
    source.write_text(_OLD)

    def refuse_to_hash(path: Path) -> str:
        raise archive_module.ArchivePublishError(f"cannot read {path} to verify it")

    monkeypatch.setattr(migrator_module, "source_digest", refuse_to_hash)
    report = migrate_dir(legacy, memory_home=home, dry_run=False, allow_llm=False)
    monkeypatch.undo()

    assert report.migrated == 0, report.migrated
    assert report.archived == 0, report.archived
    assert any("could not hash" in e for e in report.errors), report.errors
    assert _home_bodies(home) == [], _home_bodies(home)
    assert _archive_bodies(tmp_path) == [], _archive_bodies(tmp_path)
    assert source.read_text() == _OLD
    assert not (legacy / MIGRATION_MARKER_NAME).exists()


def test_a_duplicate_is_only_retired_when_its_bytes_are_the_imported_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1: the duplicate branch retired without a digest.

    Codex's route to the silent-wrong result, reached through a different branch
    than QA's: kill a run after the import, then retry. On the retry the source's
    body already matches the import, so it takes the duplicate branch — which
    archived with no digest at all. An edit after classification therefore
    retired NEW while the home kept OLD, **with no errors**.

    The duplicate branch's whole claim is "these bytes are already in the home",
    so it is the last place that claim should go unchecked.
    """
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    source = legacy / "p-note.md"
    source.write_text(_OLD)

    # 1. A kill between the import and the archive: home holds OLD, source OLD.
    def die(*args, **kwargs):  # noqa: ANN002, ANN003
        raise SystemExit("killed between the import and the archive")

    monkeypatch.setattr(migrator_module, "archive_file", die)
    with pytest.raises(SystemExit):
        migrate_dir(legacy, memory_home=home, dry_run=False, allow_llm=False)
    monkeypatch.undo()
    assert _home_bodies(home) == ["OLD"], _home_bodies(home)

    # 2. The retry, with the source edited in place after classification.
    fired = {"edit": False}
    real_link = archive_module.os.link

    def edit_before_publishing(*args, **kwargs):  # noqa: ANN002, ANN003
        if not fired["edit"]:
            fired["edit"] = True
            _edit_in_place(source, _NEW)
        return real_link(*args, **kwargs)

    monkeypatch.setattr(archive_module.os, "link", edit_before_publishing)
    retry = migrate_dir(legacy, memory_home=home, dry_run=False, allow_llm=False)
    monkeypatch.undo()

    assert fired["edit"], "the probe never fired; this test proved nothing"
    assert retry.skipped_duplicate == 1, retry.skipped_duplicate
    # THE POINT: it refuses rather than retiring bytes that are not in the home.
    assert retry.errors, "the duplicate was retired with no error at all"
    assert retry.archived == 0, retry.archived
    assert source.read_text() == _NEW, "the user's current content was retired"
    assert _archive_bodies(tmp_path) == [], _archive_bodies(tmp_path)
    # The earlier run's import is left strictly alone: this run did not write it.
    assert _home_bodies(home) == ["OLD"], _home_bodies(home)
    assert not (legacy / MIGRATION_MARKER_NAME).exists()


def test_the_undo_keeps_a_file_whose_line_endings_changed(tmp_path: Path) -> None:
    """Codex P2: the byte-identity guard was built on a text comparison.

    ``read_text`` applies universal-newline translation, so a file rewritten
    from ``hello\\n`` to ``hello\\r\\n`` read back as identical and the guard
    deleted it. The guard's entire purpose is to delete only what this run wrote
    and still holds — a normalising comparison cannot establish that.
    """
    published = tmp_path / "knowledge" / "note.md"
    published.parent.mkdir(parents=True)
    wrote = "---\nname: note\n---\n\nhello\n"
    published.write_bytes(wrote.encode("utf-8"))

    # Someone rewrote it with CRLF endings. Same characters, different bytes.
    published.write_bytes(wrote.replace("\n", "\r\n").encode("utf-8"))

    message = migrator_module._undo_import(published, wrote)
    assert "NOT removed" in message, message
    assert published.exists(), "an edited file was deleted by a text comparison"

    # The control: the unmodified bytes ARE removed, so the guard still works.
    published.write_bytes(wrote.encode("utf-8"))
    ok = migrator_module._undo_import(published, wrote)
    assert "was removed" in ok and "NOT removed" not in ok, ok
    assert not published.exists()


def test_an_interrupted_then_edited_retry_keeps_both_and_names_the_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Product's narrowed clause 3 (2026-11-07), with its reasoning.

    Codex's shape: kill a run after the import, edit the source, then retry. The
    home ends up holding both versions under ``p-note.md`` and ``p-note-1.md``.

    Product ruled that this does NOT hold the tag and that provenance to
    reconcile it must NOT be built: it needs a kill, then a user edit, then a
    retry, and nothing is lost because both versions are in the home. The
    requirement is only that the run cannot be SILENT about the pair. So this
    test asserts the one line, and asserts it reaches the CLI — a warning that
    exists only in the report object would meet the letter and not the point.
    """
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    home = _memory_home(tmp_path)
    source = legacy / "p-note.md"
    source.write_text(_OLD)

    def die(*args, **kwargs):  # noqa: ANN002, ANN003
        raise SystemExit("killed between the import and the archive")

    monkeypatch.setattr(migrator_module, "archive_file", die)
    with pytest.raises(SystemExit):
        migrate_dir(legacy, memory_home=home, dry_run=False, allow_llm=False)
    monkeypatch.undo()

    _edit_in_place(source, _NEW)
    result = _run_repo([str(legacy), "--commit", "--no-llm"], home)

    assert result.exit_code == 0, result.output
    # Both versions, in the home, where search can see them. Nothing deleted.
    assert sorted(_home_bodies(home)) == ["NEW", "OLD"], _home_bodies(home)
    # The target name is `<source-dir-slug>-p-note.md`, so match on the suffix.
    names = sorted(p.name for p in home.rglob("*p-note*.md"))
    assert len(names) == 2, names
    assert names[0].endswith("-p-note-1.md"), names
    assert names[1].endswith("-p-note.md"), names
    # And the run says so, naming the file it actually wrote.
    assert names[0] in result.output, result.output
    assert "nothing was deleted" in result.output, result.output


def test_a_source_that_cannot_be_unlinked_leaves_no_import_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2: a source-unlink ``PermissionError`` bypassed the cleanup.

    Everything verified, the archive entry published, and then the final
    ``os.unlink`` failed — an immutable flag, a sticky directory, a read-only
    mount. That arrived at the migrator as a bare ``OSError``, missed the
    ``ArchivePublishError`` branch, and landed in the outer handler: the import
    stayed in the home under a message that never mentioned it. The contract has
    to hold for every failure to retire, not only the ones with our exception
    class on them.
    """
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    source = legacy / "p-note.md"
    source.write_text(_OLD)

    real_unlink = archive_module.os.unlink

    def refuse_the_source(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if Path(path) == source:
            raise PermissionError(errno.EPERM, "operation not permitted")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(archive_module.os, "unlink", refuse_the_source)
    report = migrate_dir(legacy, memory_home=home, dry_run=False, allow_llm=False)
    monkeypatch.undo()

    assert report.migrated == 0, report.migrated
    assert report.errors, "the run reported nothing"
    message = report.errors[0]

    # THE POINT: the import is gone, and the message says so.
    assert _home_bodies(home) == [], _home_bodies(home)
    assert "import" in message and "was removed" in message, message
    # The message is true about the archive entry too, which on this path was a
    # second name for the still-present source and so was safe to retract.
    assert str(source) in message, message
    assert _archive_bodies(tmp_path) == [], _archive_bodies(tmp_path)
    # The content is still exactly where it was, and the directory is not retired.
    assert source.read_text() == _OLD
    assert not (legacy / MIGRATION_MARKER_NAME).exists()
