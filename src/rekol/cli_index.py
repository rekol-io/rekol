"""memory-index CLI: rebuild or incrementally update the vector index."""

from __future__ import annotations

import sys

import click

from rekol.cli_common import warn_skipped_files
from rekol.config import load_config
from rekol.embeddings import get_embedder
from rekol.index_lock import IndexBusyError, index_lock_path, index_write_lock
from rekol.indexer import Indexer
from rekol.store import IndexModelMismatchError, IndexStore


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
    store = IndexStore(
        db_path=cfg.index_db_path, dim=embedder.dim, embedding_model=cfg.embedding_model
    )
    # NB: do NOT reset_schema() here. The rebuild builds the new index into a
    # temp DB from scratch (fresh schema + this model's identity stamp) and
    # atomically swaps it over index.db. Wiping the live DB first would re-open
    # the very hole C2 closes — a kill mid-rebuild would leave an EMPTY live
    # index. A swapped-in DB always carries the current schema and identity, so
    # an old/legacy index is fully replaced by the swap, not by a pre-wipe.
    idx = Indexer(
        memory_root=cfg.memory_home,
        store=store,
        embedder=embedder,
        chunk_max_bytes=cfg.chunk_max_bytes,
        index_dir=cfg.index_dir,
    )
    try:
        # Serialize against a concurrent hook-fired `update` (#24): hold the
        # index-write lock across the whole build+swap so an `update` can't write
        # to the old index.db inode that the swap is about to discard. We wait
        # (blocking) for any in-flight update to finish first; the lock releases
        # automatically on process death, so it can never wedge.
        with index_write_lock(index_lock_path(cfg.index_dir), blocking=True):
            stats = idx.rebuild()
        warn_skipped_files(stats)
        click.echo(
            f"indexed {stats.files_indexed} files "
            f"({stats.chunks_written} chunks); "
            f"skipped {stats.files_skipped}"
        )
    finally:
        store.close()


@main.command()
def update() -> None:
    """Incrementally update the index for changed files.

    Only files whose content hash has changed since the last run are
    re-embedded. Deleted files are removed from the index automatically.
    """
    cfg = load_config()
    embedder = get_embedder(cfg.embedding_model)
    store = IndexStore(
        db_path=cfg.index_db_path, dim=embedder.dim, embedding_model=cfg.embedding_model
    )
    store.init_schema()
    if store.needs_schema_migration():
        store.close()
        click.echo(
            "curated index schema is out of date — run `rekol index rebuild`",
            err=True,
        )
        sys.exit(1)
    # Identity guard: an incremental update must NOT append vectors from a
    # different model into an index built by another, which would silently mix
    # incompatible vectors and return confidently-wrong search results.
    try:
        store.check_model_identity(cfg.embedding_model, embedder.dim)
    except IndexModelMismatchError as exc:
        store.close()
        click.echo(str(exc), err=True)
        sys.exit(2)
    idx = Indexer(
        memory_root=cfg.memory_home,
        store=store,
        embedder=embedder,
        chunk_max_bytes=cfg.chunk_max_bytes,
        index_dir=cfg.index_dir,
    )
    try:
        # Skip cleanly if a rebuild (or another update) holds the index-write
        # lock (#24): the running writer already covers these changes, and the
        # next update reconciles anything edited during the window. Skipping —
        # not blocking — is correct here because this command is what the
        # background auto-reindex hook runs; a blocked hook process must not hang.
        try:
            with index_write_lock(index_lock_path(cfg.index_dir), blocking=False):
                stats = idx.update()
                warn_skipped_files(stats)
                click.echo(
                    f"updated {stats.files_indexed} files, "
                    f"removed {stats.files_removed}, "
                    f"skipped {stats.files_skipped}"
                )
        except IndexBusyError:
            click.echo(
                "another rekol index operation is in progress — skipping this "
                "update; the running operation will cover these changes.",
                err=True,
            )
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
