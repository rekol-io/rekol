"""Shared helpers across the rekol CLI commands."""

from __future__ import annotations

import click

from rekol.store import IndexStore


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
