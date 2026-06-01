"""`rekol review` — re-confirm durable memories overdue past the interval.

Exempt layers (always/, knowledge/) never decay out of recall, so this lets the
user confirm (bump ``updated``), invalidate, or skip each overdue one. ``--nudge``
prints a one-line reminder (for the SessionEnd hook); ``--list`` is non-interactive.
"""

from __future__ import annotations

import datetime as dt

import click
import frontmatter

from rekol.config import Config, load_config
from rekol.review import find_overdue
from rekol.store import IndexStore


def _overdue(cfg: Config) -> list[dict]:
    """Return the overdue durable memories for ``cfg`` (reads the curated index)."""
    store = IndexStore(db_path=cfg.index_db_path, use_sqlite_vec=False)
    store.init_schema()
    try:
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


@click.command()
@click.option("--nudge", is_flag=True, help="Print a one-line reminder iff overdue; for hooks.")
@click.option("--list", "as_list", is_flag=True, help="Print overdue file paths (non-interactive).")
def main(nudge: bool, as_list: bool) -> None:
    """Review durable (always/, knowledge/) memories overdue for confirmation."""
    cfg = load_config()
    overdue = _overdue(cfg)
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
    for item in overdue:
        click.echo(f"{item['file_path']} (updated {item['updated'] or 'never'})")
        choice = click.prompt("[c]onfirm / [i]nvalidate / [s]kip", default="s").strip().lower()
        if choice.startswith("c"):
            post = frontmatter.load(item["file_path"])
            post["updated"] = dt.date.today().isoformat()
            with open(item["file_path"], "w", encoding="utf-8") as handle:
                handle.write(frontmatter.dumps(post))
            click.echo("  confirmed")
        elif choice.startswith("i"):
            click.echo(f"  to invalidate, run: rekol invalidate {item['file_path']}")
