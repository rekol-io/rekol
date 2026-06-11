"""`rekol review` — re-confirm durable memories overdue past the interval.

Exempt layers (always/, knowledge/) never decay out of recall, so this lets the
user confirm (bump ``updated``), invalidate, or skip each overdue one. ``--nudge``
prints a one-line reminder (for the SessionEnd hook); ``--list`` is non-interactive.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import click
import frontmatter

from rekol.config import Config, load_config
from rekol.review import find_overdue
from rekol.store import CuratedSchemaOutdatedError, IndexStore


def _overdue(cfg: Config) -> list[dict]:
    """Return the overdue durable memories for ``cfg`` (reads the curated index).

    Raises :class:`CuratedSchemaOutdatedError` when the index predates the
    timestamp columns — the caller decides whether to instruct or soft-fail.
    """
    store = IndexStore(db_path=cfg.index_db_path, use_sqlite_vec=False)
    store.init_schema()
    try:
        if store.needs_schema_migration():
            raise CuratedSchemaOutdatedError()
        rows = store.distinct_file_timestamps()
    finally:
        store.close()
    return find_overdue(
        rows,
        memory_home=cfg.memory_home,
        exempt_layers=cfg.temporal_recency_exempt_layers,
        interval_days=cfg.temporal_confirm_interval_days,
        today=dt.date.today(),
    )


def _confirm(file_path: str, cfg: Config) -> bool:
    """Stamp ``last_confirmed`` to today on the memory file; return True on success.

    Confirmation is DISTINCT from an edit (#87): it bumps ``last_confirmed``, not
    ``updated`` — so "I verified this still holds" is recorded without masquerading
    as a content change. A confirmation also clears any prior suspect flag.

    The path comes from the curated index, so confine it to ``$MEMORY_HOME``
    (resolving ``..``) before writing — a poisoned/synced index row must not be
    able to redirect the write outside the memory root. Mirrors ``rekol invalidate``.
    """
    target = Path(file_path).resolve()
    try:
        target.relative_to(cfg.memory_home.resolve())
    except ValueError:
        click.echo(f"  refused: {target} is outside {cfg.memory_home}", err=True)
        return False
    post = frontmatter.load(str(target))
    post["last_confirmed"] = dt.date.today().isoformat()
    post.metadata.pop("suspected_at", None)
    post.metadata.pop("suspect_reason", None)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(frontmatter.dumps(post))
    return True


def _interactive(overdue: list[dict], cfg: Config) -> None:
    """Prompt confirm/invalidate/skip for each overdue durable memory."""
    confirmed = 0
    for item in overdue:
        last = item.get("last_confirmed") or item.get("updated") or "never"
        click.echo(f"{item['file_path']} (last confirmed {last})")
        choice = click.prompt("[c]onfirm / [i]nvalidate / [s]kip", default="s").strip().lower()
        if choice.startswith("c"):
            if _confirm(item["file_path"], cfg):
                confirmed += 1
                click.echo("  confirmed")
        elif choice.startswith("i"):
            click.echo(f"  to invalidate, run: rekol invalidate {item['file_path']}")
    if confirmed:
        # Confirm writes `last_confirmed` to the markdown; the curated index keeps the
        # old value (and thus the [review?] tag) until the next reindex.
        click.echo(f"confirmed {confirmed} — run `rekol index update` to refresh search tags")


@click.command()
@click.option("--nudge", is_flag=True, help="Print a one-line reminder iff overdue; for hooks.")
@click.option("--list", "as_list", is_flag=True, help="Print overdue file paths (non-interactive).")
def main(nudge: bool, as_list: bool) -> None:
    """Review durable (always/, knowledge/) memories overdue for confirmation."""
    cfg = load_config()
    try:
        overdue = _overdue(cfg)
    except CuratedSchemaOutdatedError:
        if nudge:
            return  # soft-fail: never noise the SessionEnd hook
        click.echo("curated index schema is out of date — run `rekol index rebuild`", err=True)
        raise SystemExit(1) from None

    if nudge:
        if overdue:
            click.echo(
                f"[rekol] {len(overdue)} durable memories are due for review — run `rekol review`"
            )
        return
    if not overdue:
        click.echo("All durable memories are within the confirmation interval.")
        return
    if as_list:
        for item in overdue:
            click.echo(item["file_path"])
        return
    _interactive(overdue, cfg)
