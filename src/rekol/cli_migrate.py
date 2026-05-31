"""rekol migrate CLI — migrate legacy memory into $REKOL_HOME (or $MEMORY_HOME).

Subcommands:
  auto           Scan ~/.claude/projects/*/memory/ for legacy files (default).
  repo <path>    Scan a user-supplied repo or directory for legacy files.
  redo <file>    Re-classify a single previously-archived file.

Modes:
  --dry-run      Print what would happen, write nothing. Default.
  --commit       Actually perform the migration.
  --no-llm       Disable the Sonnet fallback; unknown files default to knowledge/.
  --quiet        Suppress per-file output; still prints the summary line.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from rekol.config import resolve_memory_home
from rekol.migrate.discover import discover_auto_memory_sources
from rekol.migrate.migrator import MigrationReport, migrate_dir


def _memory_home() -> Path:
    # Same precedence as load_config(): REKOL_HOME primary, MEMORY_HOME fallback.
    env = resolve_memory_home()
    if not env:
        click.echo("error: neither REKOL_HOME nor MEMORY_HOME is set.", err=True)
        sys.exit(2)
    return Path(os.path.expanduser(env))


def _print_report(report: MigrationReport, label: str, dry_run: bool, quiet: bool) -> None:
    if dry_run:
        if not quiet or report.would_migrate:
            click.echo(
                f"{label}: would migrate {report.would_migrate} "
                f"(heuristic={report.by_heuristic}, llm={report.by_llm})"
            )
    else:
        if not quiet or report.migrated:
            click.echo(
                f"{label}: migrated {report.migrated} "
                f"(heuristic={report.by_heuristic}, llm={report.by_llm}, "
                f"archived={report.archived})"
            )
    if report.skipped_retired:
        click.echo(f"{label}: skipped — already retired")
    if report.skipped_missing:
        click.echo(f"{label}: skipped — source dir missing")
    for err in report.errors:
        click.echo(f"{label}: ERROR {err}", err=True)


@click.group()
def main() -> None:
    """Migrate legacy Claude memory files into $REKOL_HOME (or $MEMORY_HOME)."""


def _mode_flags() -> tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]]:
    """Shared option set for commit/dry-run mode."""
    return (
        click.option(
            "--dry-run/--commit",
            default=True,
            show_default=True,
            help="Dry-run prints a plan; --commit writes files.",
        ),
        click.option(
            "--no-llm",
            is_flag=True,
            default=False,
            help="Disable Sonnet classifier fallback; default unknown files to knowledge/.",
        ),
        click.option("--quiet", is_flag=True, default=False, help="Suppress per-file output."),
    )


def _add_mode_flags(cmd):
    # Apply shared flags via decorator stacking — click requires this form
    cmd = click.option("--quiet", is_flag=True, default=False)(cmd)
    cmd = click.option("--no-llm", is_flag=True, default=False)(cmd)
    cmd = click.option("--dry-run/--commit", default=True, show_default=True)(cmd)
    return cmd


@main.command()
@click.option(
    "--dry-run/--commit",
    default=True,
    show_default=True,
    help="Dry-run prints a plan; --commit writes files.",
)
@click.option(
    "--no-llm",
    is_flag=True,
    default=False,
    help="Disable Sonnet fallback; default unknown files to knowledge/.",
)
@click.option("--quiet", is_flag=True, default=False, help="Suppress per-file output.")
def auto(dry_run: bool, no_llm: bool, quiet: bool) -> None:
    """Scan ~/.claude/projects/*/memory/ and migrate each unmigrated dir."""
    memory_home = _memory_home()
    slug_dirs = discover_auto_memory_sources()
    if not slug_dirs:
        if not quiet:
            click.echo("nothing to migrate: no legacy memory directories found.")
        return
    for slug_dir in slug_dirs:
        source = slug_dir / "memory"
        label = slug_dir.name
        report = migrate_dir(
            source_dir=source,
            memory_home=memory_home,
            dry_run=dry_run,
            allow_llm=not no_llm,
        )
        _print_report(report, label, dry_run, quiet)


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=True, dir_okay=True))
@click.option(
    "--dry-run/--commit",
    default=True,
    show_default=True,
    help="Dry-run prints a plan; --commit writes files.",
)
@click.option(
    "--no-llm",
    is_flag=True,
    default=False,
    help="Disable Sonnet fallback; default unknown files to knowledge/.",
)
@click.option("--quiet", is_flag=True, default=False, help="Suppress per-file output.")
def repo(path: str, dry_run: bool, no_llm: bool, quiet: bool) -> None:
    """Scan a user-supplied path for legacy memory files.

    PATH may be either:
      - a directory that directly contains .md memory files; or
      - a directory whose ``memory/`` subdir contains them.
    """
    memory_home = _memory_home()
    target = Path(path)
    # Prefer target/memory/ if present; else treat target itself as the memory dir.
    candidate = target / "memory"
    source = candidate if candidate.is_dir() else target

    report = migrate_dir(
        source_dir=source,
        memory_home=memory_home,
        dry_run=dry_run,
        allow_llm=not no_llm,
    )
    _print_report(report, target.name, dry_run, quiet)
