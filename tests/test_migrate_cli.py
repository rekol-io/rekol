"""Tests for memory-migrate CLI subcommands."""

from __future__ import annotations

import shutil
from pathlib import Path

from click.testing import CliRunner

from rekol.cli_migrate import main

FIXTURES = Path(__file__).parent / "fixtures" / "legacy-project" / "memory"


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    src_parent = tmp_path / "proj"
    src_parent.mkdir()
    shutil.copytree(FIXTURES, src_parent / "memory")
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)
    (memory_home / "INDEX.md").write_text("# Index\n")
    return src_parent, memory_home


def test_cli_repo_dry_run(tmp_path: Path, monkeypatch) -> None:
    src_parent, memory_home = _setup(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "repo",
            str(src_parent),
            "--dry-run",
            "--no-llm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "would migrate" in result.output.lower()
    # No files actually written
    assert not any((memory_home / "topics").iterdir())


def test_cli_repo_commit(tmp_path: Path, monkeypatch) -> None:
    src_parent, memory_home = _setup(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "repo",
            str(src_parent),
            "--commit",
            "--no-llm",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (memory_home / "topics" / "proj-alpha.md").is_file()
    assert (memory_home / "when" / "when-proj-foo.md").is_file()


def test_cli_auto_no_legacy_is_noop(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    memory_home = tmp_path / "MEMORY_HOME"
    (memory_home / "topics").mkdir(parents=True)
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    runner = CliRunner()
    result = runner.invoke(main, ["auto", "--commit", "--no-llm", "--quiet"])
    assert result.exit_code == 0, result.output
    assert "nothing to migrate" in result.output.lower() or result.output.strip() == ""


def test_cli_auto_finds_project_dirs(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-Users-x-proj-a" / "memory"
    proj.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "project_alpha.md", proj / "project_alpha.md")
    shutil.copyfile(FIXTURES / "MEMORY.md", proj / "MEMORY.md")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True)
    (memory_home / "INDEX.md").write_text("# Index\n")
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))

    runner = CliRunner()
    result = runner.invoke(main, ["auto", "--commit", "--no-llm"])
    assert result.exit_code == 0, result.output
    # Classified as topic (type: project → topic)
    # Slug becomes -Users-x-proj-a
    assert any((memory_home / "topics").iterdir())


def test_cli_requires_memory_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    runner = CliRunner()
    result = runner.invoke(main, ["auto", "--commit"])
    assert result.exit_code != 0
    assert "MEMORY_HOME" in result.output


# ------------------------------- #166 exit codes ------------------------------
# These test the CLI BOUNDARY, not the report object. The original defect was
# "exit 0 -> install.sh journals MIGRATED over a failed migration", so asserting
# only on `MigrationReport` leaves the thing that actually broke unguarded.
#
# The first attempt at this coverage was a test named `..._exits_zero` that
# called `migrate_dir` directly and never invoked Click — it could not observe an
# exit code at all, while its name claimed it did. Caught in external review.


def _plain_source(tmp_path: Path) -> tuple[Path, Path]:
    """A source dir whose single file has no frontmatter → defaults to knowledge/."""
    src_parent = tmp_path / "proj"
    (src_parent / "memory").mkdir(parents=True)
    (src_parent / "memory" / "plain.md").write_text("nothing to classify on\n")
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)
    return src_parent, memory_home


def test_cli_repo_defaulted_no_llm_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """A defaulted file is an IMPORT: exit 0, marker written, warning shown.

    `--no-llm` is what install.sh and cli_init both pass, so this is the default
    production path. Exit 0 here is deliberate policy, asserted at the boundary
    the installer actually reads.
    """
    src_parent, memory_home = _plain_source(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    result = CliRunner().invoke(main, ["repo", str(src_parent / "memory"), "--commit", "--no-llm"])
    assert result.exit_code == 0, result.output
    assert "defaulted=1" in result.output
    assert "could not be classified" in result.output
    # ...and it must NOT promise a rerun that cannot work.
    assert "will NOT reclassify" in result.output
    from rekol.migrate.archive import MIGRATION_MARKER_NAME

    assert (src_parent / "memory" / MIGRATION_MARKER_NAME).is_file()


def test_cli_repo_hard_failure_exits_one(tmp_path: Path, monkeypatch) -> None:
    """An unimported file must make the CLI exit non-zero.

    This is the boundary install.sh reads: `if rekol migrate ... ; then
    log_journal "MIGRATED"`. A zero exit here is what let the durable install
    record claim success over a migration in which nothing worked.
    """
    src_parent, memory_home = _plain_source(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))

    from rekol.migrate import migrator as migrator_mod

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated classification failure")

    monkeypatch.setattr(migrator_mod, "classify_file", boom)
    result = CliRunner().invoke(main, ["repo", str(src_parent / "memory"), "--commit", "--no-llm"])
    assert result.exit_code == 1, result.output
    assert "ERROR" in result.output
    from rekol.migrate.archive import MIGRATION_MARKER_NAME

    assert not (src_parent / "memory" / MIGRATION_MARKER_NAME).exists(), (
        "an unimported file must leave the directory retryable"
    )


def test_cli_repo_mixed_outcome_exits_one_and_stays_retryable(tmp_path: Path, monkeypatch) -> None:
    """Partial success: exit 1, no marker, and the failed original still present."""
    src_parent = tmp_path / "proj"
    (src_parent / "memory").mkdir(parents=True)
    (src_parent / "memory" / "good.md").write_text("imports fine\n")
    (src_parent / "memory" / "bad.md").write_text("will blow up\n")
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))

    from rekol.migrate import migrator as migrator_mod

    original = migrator_mod.classify_file

    def selective(lf, **kwargs):  # type: ignore[no-untyped-def]
        if lf.source_path.name == "bad.md":
            raise RuntimeError("simulated classification failure")
        return original(lf, **kwargs)

    monkeypatch.setattr(migrator_mod, "classify_file", selective)
    result = CliRunner().invoke(main, ["repo", str(src_parent / "memory"), "--commit", "--no-llm"])
    assert result.exit_code == 1, result.output
    from rekol.migrate.archive import MIGRATION_MARKER_NAME

    assert not (src_parent / "memory" / MIGRATION_MARKER_NAME).exists()
    assert (src_parent / "memory" / "bad.md").is_file(), "failed original must remain retryable"


def test_cli_auto_mixed_dirs_exits_one_and_keeps_failures_retryable(
    tmp_path: Path, monkeypatch
) -> None:
    """`auto` must carry an error ACROSS directories and exit 1 after processing all.

    This is the command the installer actually runs
    (`rekol migrate auto --commit --no-llm`), and it has aggregation logic that
    `repo` does not:

        any_errors = False
        for slug_dir in slug_dirs: ...; if report.errors: any_errors = True
        if any_errors: sys.exit(1)

    The three `repo` tests cannot prove that. Testing the library while leaving
    the shipped caller unproven is precisely the pattern that caused this whole
    review cycle — the original defect was "report correct, installer journals
    MIGRATED anyway".

    Two discovered directories: one defaults cleanly (retirable), one raises
    (must stay retryable). Asserts the run is not fail-fast and not
    order-dependent — the good directory is still processed.
    """
    from rekol.migrate.archive import MIGRATION_MARKER_NAME

    home = tmp_path / "home"
    good = home / ".claude" / "projects" / "-Users-x-good" / "memory"
    bad = home / ".claude" / "projects" / "-Users-x-bad" / "memory"
    good.mkdir(parents=True)
    bad.mkdir(parents=True)
    (good / "ok.md").write_text("imports fine\n")
    (bad / "boom.md").write_text("will blow up\n")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    memory_home = tmp_path / "MEMORY_HOME"
    for layer in ("always", "when", "topics", "knowledge"):
        (memory_home / layer).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    # REKOL_HOME is primary and would silently outrank MEMORY_HOME — the exact
    # precedence that sent a hand-run verification at the real memory home.
    monkeypatch.delenv("REKOL_HOME", raising=False)

    from rekol.migrate import migrator as migrator_mod

    original = migrator_mod.classify_file

    def selective(lf, **kwargs):  # type: ignore[no-untyped-def]
        if lf.source_path.name == "boom.md":
            raise RuntimeError("simulated classification failure")
        return original(lf, **kwargs)

    monkeypatch.setattr(migrator_mod, "classify_file", selective)

    result = CliRunner().invoke(main, ["auto", "--commit", "--no-llm"])

    assert result.exit_code == 1, result.output

    # The failing directory stays retryable...
    assert not (bad / MIGRATION_MARKER_NAME).exists(), (
        "a directory with an unimported file must not be retired"
    )
    assert (bad / "boom.md").is_file(), "the failed original must remain for a retry"

    # ...and the healthy one was still processed, so aggregation is not fail-fast.
    assert (good / MIGRATION_MARKER_NAME).is_file(), (
        "auto must keep processing after a failure in another directory"
    )
