"""Shared helpers across the rekol CLI commands."""

from __future__ import annotations

import click

from rekol.indexer import IndexStats
from rekol.store import IndexStore


def warn_skipped_files(stats: IndexStats) -> None:
    """Emit a loud stderr warning naming files skipped for bad frontmatter.

    Issue #34: the indexer skips any file whose required ``name``/``description``/
    ``type`` frontmatter is missing or invalid, but previously surfaced only a
    bare count buried in the summary line. A user hand-authoring memory files
    then gets an empty (or near-empty) index and "search returns nothing" with
    no signal which file is wrong. Make a non-zero skip visible and actionable:
    name each offending file and the reason, on stderr, so it stands out from
    the normal stdout summary. No-op when nothing was skipped.
    """
    if not stats.skipped_files:
        return
    count = len(stats.skipped_files)
    noun = "file" if count == 1 else "files"
    click.echo(
        f"⚠ skipped {count} {noun} (invalid/missing frontmatter — "
        "required: name, description, type):",
        err=True,
    )
    for path, reason in stats.skipped_files:
        click.echo(f"    {path} — {reason}", err=True)


def guard_curated_schema(store: IndexStore) -> None:
    """Exit with an actionable message if the curated index predates the timestamp columns.

    Any command that opens the curated store and then reads/writes the timestamp
    columns must call this right after ``init_schema()`` — on a legacy index the
    columns are absent and the query/insert would otherwise crash. Closes the
    store before raising ``SystemExit(1)``.
    """
    if store.needs_schema_migration():
        store.close()
        click.echo(
            "curated index schema is out of date — run `rekol index rebuild`",
            err=True,
        )
        raise SystemExit(1)
