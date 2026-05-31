"""memory-index CLI: rebuild or incrementally update the vector index."""

from __future__ import annotations

import sys

import click

from memory_tools.config import load_config
from memory_tools.embeddings import get_embedder
from memory_tools.indexer import Indexer
from memory_tools.store import IndexStore


@click.group()
def main() -> None:
    """Manage the memory vector index."""


@main.command()
def rebuild() -> None:
    """Drop and rebuild the full index from scratch.

    All existing index data is cleared before reindexing, making this
    suitable for recovery or after bulk changes to memory files.
    """
    cfg = load_config()
    embedder = get_embedder(cfg.embedding_model)
    store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
    store.init_schema()
    idx = Indexer(
        memory_root=cfg.memory_home,
        store=store,
        embedder=embedder,
        chunk_max_bytes=cfg.chunk_max_bytes,
    )
    stats = idx.rebuild()
    click.echo(
        f"indexed {stats.files_indexed} files "
        f"({stats.chunks_written} chunks); "
        f"skipped {stats.files_skipped}"
    )


@main.command()
def update() -> None:
    """Incrementally update the index for changed files.

    Only files whose content hash has changed since the last run are
    re-embedded. Deleted files are removed from the index automatically.
    """
    cfg = load_config()
    embedder = get_embedder(cfg.embedding_model)
    store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
    store.init_schema()
    idx = Indexer(
        memory_root=cfg.memory_home,
        store=store,
        embedder=embedder,
        chunk_max_bytes=cfg.chunk_max_bytes,
    )
    stats = idx.update()
    click.echo(
        f"updated {stats.files_indexed} files, "
        f"removed {stats.files_removed}, "
        f"skipped {stats.files_skipped}"
    )


if __name__ == "__main__":
    sys.exit(main())
