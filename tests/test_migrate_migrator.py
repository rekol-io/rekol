"""End-to-end tests for the migrator orchestrator."""

from __future__ import annotations

import shutil
from pathlib import Path

from rekol.migrate.archive import MIGRATION_MARKER_NAME
from rekol.migrate.migrator import (
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


def test_migrate_dir_does_NOT_retire_when_every_file_fails(tmp_path: Path) -> None:
    """A totally-failed migration must NOT be marked retired (#166).

    This test previously asserted the OPPOSITE — the marker was written even when
    every file failed, to stop a broken corpus being retried on every install. That
    trade was wrong: the tombstone makes a re-run print "skipped — already retired",
    so the files sit un-migrated in their original directory and rekol never looks
    at them again. **The user's legacy memory is abandoned, and the run reports
    success.** The documented remedy (re-run `rekol migrate auto --commit`) is a
    guaranteed no-op once the tombstone exists.

    A repeated retry is visible and recoverable; an unnoticed tombstone is neither.
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

    from rekol.migrate import classify

    original = classify.classify_file

    def failing_classify(*args, **kwargs):
        raise RuntimeError("simulated classification failure")

    classify.classify_file = failing_classify
    # The migrator imports classify_file by name at module load — patch it there too.
    from rekol.migrate import migrator

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
    # The dir must remain un-retired so a later run (with the LLM available, or
    # after the corpus is fixed) can actually migrate it.
    assert not (src / MIGRATION_MARKER_NAME).exists(), (
        "a run in which every file failed must not tombstone the source dir — "
        "that abandons the user's memory while reporting success"
    )


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


# --------------------------------- #166 --------------------------------------


def test_llm_failure_is_recorded_not_swallowed(tmp_path: Path) -> None:
    """An unavailable LLM must appear in the report, not vanish.

    `except LLMUnavailable: pass` meant a run where the LLM was down for every
    file produced a full set of stub classifications with NOTHING printed and
    nothing in report.errors — printed as `migrated N (heuristic=N, llm=0)`,
    indistinguishable from a real frontmatter-driven success.
    """
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)

    src = tmp_path / "proj" / "memory"
    src.mkdir(parents=True)
    (src / "note.md").write_text("no frontmatter here, just a body\n")

    from rekol.migrate import classify as classify_mod

    def unavailable(*args: object, **kwargs: object) -> dict:
        raise classify_mod.LLMUnavailable("claude CLI not on PATH")

    original = classify_mod.call_claude_classifier
    classify_mod.call_claude_classifier = unavailable  # type: ignore[assignment]
    try:
        report = migrate_dir(source_dir=src, memory_home=memory_home, dry_run=False, allow_llm=True)
    finally:
        classify_mod.call_claude_classifier = original  # type: ignore[assignment]

    assert report.migrated == 1
    # Counted as defaulted, NOT as a heuristic success.
    assert report.by_defaulted == 1
    assert report.by_heuristic == 0
    # The reason is recorded rather than swallowed — as a WARNING, not an error.
    # `errors` means "not imported" and blocks retirement; a defaulted file WAS
    # imported, so recording it as an error made this path disagree with --no-llm
    # about whether the directory may be retired, for an identical on-disk result.
    assert any("LLM classification unavailable" in w for w in report.warnings), report.warnings
    assert not report.errors, "a defaulted file is imported, not failed"


def test_defaulted_files_do_not_count_as_heuristic(tmp_path: Path) -> None:
    """`--no-llm` defaults are still defaults — the tally must say so.

    install.sh and cli_init both pass --no-llm, so this is the DEFAULT install
    path: every unclassifiable file lands in knowledge/ with a stub description.
    Reporting those as `heuristic=N` hid how much of a migration was guesswork.
    """
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)

    src = tmp_path / "proj" / "memory"
    src.mkdir(parents=True)
    (src / "a.md").write_text("plain body, nothing to classify on\n")

    report = migrate_dir(source_dir=src, memory_home=memory_home, dry_run=False, allow_llm=False)

    assert report.migrated == 1
    assert report.by_defaulted == 1
    assert report.by_heuristic == 0


def test_ordinary_success_retires(tmp_path: Path) -> None:
    """Narrowing the tombstone must not remove it: a clean run still retires.

    Renamed from `test_a_partially_successful_run_still_retires`, which was a
    VACUOUS test — it created one file, let it succeed, and asserted the marker.
    There was no partial failure in it at all, so it proved ordinary success
    retires while its name claimed it proved partial success was safe. Caught in
    external review; it is the same shape as every bug this file guards against.
    """
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)

    src = tmp_path / "proj" / "memory"
    src.mkdir(parents=True)
    (src / "ok.md").write_text("a body that migrates fine\n")

    report = migrate_dir(source_dir=src, memory_home=memory_home, dry_run=False, allow_llm=False)

    assert report.migrated == 1
    assert not report.errors
    assert (src / MIGRATION_MARKER_NAME).is_file()


def test_partial_success_does_NOT_retire(tmp_path: Path) -> None:
    """ONE failed file must block retirement of the whole directory.

    The real partial-success case, and the one the first fix missed. Removing
    `len(errors) > 0` from the condition narrowed the bug from "every file
    failed" to "at least one file succeeded" — with `migrated > 0` alone, one
    good file tombstones the directory and every failed sibling is abandoned
    permanently, because the next run prints "skipped — already retired".

    Successful originals have already moved to old-memory-archive/, so leaving
    the directory unretired means a re-run sees exactly the files that still
    need work.
    """
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)

    src = tmp_path / "proj" / "memory"
    src.mkdir(parents=True)
    (src / "good.md").write_text("this one imports fine\n")
    (src / "bad.md").write_text("this one will blow up\n")

    from rekol.migrate import classify as classify_mod
    from rekol.migrate import migrator as migrator_mod

    original = migrator_mod.classify_file

    def selective(lf, **kwargs):  # type: ignore[no-untyped-def]
        if lf.source_path.name == "bad.md":
            raise RuntimeError("simulated classification failure")
        return original(lf, **kwargs)

    classify_mod.classify_file = selective  # type: ignore[assignment]
    migrator_mod.classify_file = selective  # type: ignore[assignment]
    try:
        report = migrate_dir(
            source_dir=src, memory_home=memory_home, dry_run=False, allow_llm=False
        )
    finally:
        classify_mod.classify_file = original  # type: ignore[assignment]
        migrator_mod.classify_file = original  # type: ignore[assignment]

    assert report.migrated == 1, "the good file should still import"
    assert len(report.errors) == 1, "the bad file should be recorded"
    assert not (src / MIGRATION_MARKER_NAME).exists(), (
        "one unimported file must block retirement of the whole directory — "
        "otherwise the failed sibling is abandoned permanently"
    )
    # The failed original is still where a retry will find it.
    assert (src / "bad.md").is_file()


def test_defaulted_under_no_llm_still_retires_and_exits_zero(tmp_path: Path) -> None:
    """Policy, asserted explicitly: a defaulted file is IMPORTED, not failed.

    `--no-llm` is what install.sh and cli_init both pass, so this is the default
    production path. A defaulted file has its body preserved and searchable; it is
    merely poorly described. Blocking retirement on it would make every ordinary
    install retry forever.

    This test exists to make that a DECISION rather than an accident — the first
    fix left the two paths disagreeing: LLM-unavailable recorded an error (exit 1)
    while --no-llm did not (exit 0), for the same durable outcome.
    """
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)

    src = tmp_path / "proj" / "memory"
    src.mkdir(parents=True)
    (src / "plain.md").write_text("nothing to classify on\n")

    report = migrate_dir(source_dir=src, memory_home=memory_home, dry_run=False, allow_llm=False)

    assert report.by_defaulted == 1
    assert not report.errors, "a defaulted file is an import, not a failure"
    assert (src / MIGRATION_MARKER_NAME).is_file()


def test_llm_unavailable_records_the_reason_but_still_retires(tmp_path: Path) -> None:
    """The two defaulting paths must reach the SAME durable state.

    LLM-attempted-and-unavailable records why (useful diagnostics), but the file
    is imported exactly as in the --no-llm case, so retirement must agree. The
    first fix had these disagree, which is how the production path stayed broken
    while the tested path looked fixed.
    """
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)

    src = tmp_path / "proj" / "memory"
    src.mkdir(parents=True)
    (src / "note.md").write_text("no frontmatter here\n")

    from rekol.migrate import classify as classify_mod

    def unavailable(*args: object, **kwargs: object) -> dict:
        raise classify_mod.LLMUnavailable("claude CLI not on PATH")

    original = classify_mod.call_claude_classifier
    classify_mod.call_claude_classifier = unavailable  # type: ignore[assignment]
    try:
        report = migrate_dir(source_dir=src, memory_home=memory_home, dry_run=False, allow_llm=True)
    finally:
        classify_mod.call_claude_classifier = original  # type: ignore[assignment]

    assert report.by_defaulted == 1
    assert (src / MIGRATION_MARKER_NAME).is_file(), (
        "must reach the same durable state as the --no-llm path"
    )
