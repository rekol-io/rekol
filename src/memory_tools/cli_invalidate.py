"""memory-invalidate CLI: mark a memory file as invalidated without deleting it.

Bi-temporal model: a memory's facts may stop being true (Phase 2 hyperparameters
change, a service URL is decommissioned, a person leaves the team) but the
historical claim remains useful — "what did I believe in March?" is a real
query.  Invalidation sets ``invalidated_at`` in the frontmatter and bumps
``updated``.  The file stays on disk and remains searchable, but downstream
retrieval can de-prioritize invalidated memories.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import click
import frontmatter

from memory_tools.config import load_config
from memory_tools.embeddings import get_embedder
from memory_tools.indexer import Indexer
from memory_tools.store import IndexStore


@click.command()
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--reason",
    default="",
    help="Optional one-line note describing why the memory is being "
    "invalidated.  Appended as a body comment.",
)
@click.option(
    "--at",
    "invalidated_at",
    help="ISO-8601 datetime (or date) the memory's facts stopped being true. Defaults to now.",
)
def main(file_path: str, reason: str, invalidated_at: str | None) -> None:
    """Mark FILE_PATH as invalidated (set ``invalidated_at`` in frontmatter).

    The file stays on disk and in the search index — invalidation is a soft
    state, not a delete.  Use ``rm`` for hard removal.
    """
    cfg = load_config()
    target = Path(file_path).resolve()

    # Refuse to invalidate files outside MEMORY_HOME — paranoia against typos.
    try:
        target.relative_to(cfg.memory_home.resolve())
    except ValueError as exc:
        raise click.ClickException(
            f"{target} is not under MEMORY_HOME ({cfg.memory_home})"
        ) from exc

    post = frontmatter.load(target)
    if post.metadata.get("invalidated_at"):
        raise click.ClickException(
            f"{target} is already invalidated "
            f"(at {post.metadata['invalidated_at']!r}); to revive, edit the "
            f"file and remove the field."
        )

    # Bump both timestamps to a precise datetime so chronological ordering
    # across same-day invalidations is unambiguous.  ``--at`` accepts either
    # a date or full datetime string and is passed through unchanged.
    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    today_date = dt.date.today().isoformat()
    post.metadata["invalidated_at"] = invalidated_at or now_iso
    post.metadata["updated"] = now_iso

    if reason:
        post.content = post.content.rstrip() + f"\n\n<!-- invalidated {today_date}: {reason} -->\n"

    target.write_text(frontmatter.dumps(post) + "\n")
    click.echo(f"invalidated {target} (at={post.metadata['invalidated_at']})")

    # Reindex so the new frontmatter is picked up.  The body changed if a
    # reason was supplied, so the indexer would re-embed anyway.
    embedder = get_embedder(cfg.embedding_model)
    store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
    store.init_schema()
    idx = Indexer(
        memory_root=cfg.memory_home,
        store=store,
        embedder=embedder,
        chunk_max_bytes=cfg.chunk_max_bytes,
    )
    idx.update()


if __name__ == "__main__":
    sys.exit(main())
