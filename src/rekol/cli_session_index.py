"""claude-session-index: ingest Claude Code transcripts into the sessions DB.

Two modes:
  --full         Walk every JSONL under claude_projects_dir and re-ingest,
                 bypassing the per-file mtime+size skip. Used for backfill
                 or after a schema change invalidates the existing index.
  --incremental  (default) Same walk, but trust ``files_seen`` to skip
                 files whose mtime+size match what was last ingested.
                 This is the steady-state SessionEnd-hook mode.

Both modes share the same code path in ``ingest_directory``; the only
difference is the ``force`` flag controlling whether ``ingest_file``
honours the ``files_seen`` skip.
"""

from __future__ import annotations

import sys

import click

from rekol.config import load_config
from rekol.embeddings import get_embedder
from rekol.sessions.ingest import embed_missing, ingest_directory
from rekol.sessions.store import SessionStore, SessionStoreDimMismatchError


@click.command()
@click.option(
    "--full",
    "mode_full",
    is_flag=True,
    help="Full reingest of all transcripts. Forces re-walk even of files whose "
    "mtime+size match what was last ingested (vs --incremental, which trusts "
    "files_seen and skips them).",
)
@click.option(
    "--incremental",
    "mode_incremental",
    is_flag=True,
    help="Incremental reingest (default behaviour). Skips files whose mtime+size "
    "match what was last ingested; this is what makes the SessionEnd hook fast.",
)
@click.option(
    "--embed/--no-embed",
    default=True,
    show_default=True,
    help="Compute local vector embeddings for new messages so transcript search "
    "is semantic, not keyword-only. On by default (the SessionEnd hook path "
    "must be semantic); pass --no-embed for a faster FTS5-only ingest.",
)
@click.option(
    "--progress/--no-progress",
    default=True,
    show_default=True,
    help="Print a one-line counter to stderr every 50 files so a multi-minute "
    "backfill is not silent.",
)
def main(mode_full: bool, mode_incremental: bool, embed: bool, progress: bool) -> None:
    """Ingest ~/.claude/projects/*/*.jsonl into the sessions search DB."""
    if mode_full and mode_incremental:
        raise click.UsageError("--full and --incremental are mutually exclusive")
    cfg = load_config()
    if not cfg.session_search_enabled:
        click.echo(
            "session_search_enabled=false in config; nothing to do. "
            "Set session_search_enabled: true in rekol.config.yaml to enable."
        )
        sys.exit(0)
    projects_root = cfg.claude_projects_dir
    if not projects_root.is_dir():
        click.echo(f"claude_projects_dir does not exist: {projects_root}", err=True)
        sys.exit(2)

    # Build the embedder only AFTER the projects-dir guard above, so the
    # missing-dir / disabled-config exit paths never pay the model load cost.
    # Uses the same local model as curated-memory search (cfg.embedding_model),
    # so transcript and memory search share one semantic space.
    embedder = get_embedder(cfg.embedding_model) if embed else None
    store_dim = embedder.dim if embedder is not None else 384

    # Progress callback prints to stderr so it doesn't interleave with the
    # final stats line on stdout (tests assert against stdout substrings).
    progress_cb = None
    if progress:

        def _emit_progress(done: int, total: int) -> None:
            click.echo(f"... {done}/{total} files indexed", err=True)

        progress_cb = _emit_progress

    repaired = 0
    try:
        with SessionStore(db_path=cfg.sessions_db_path, dim=store_dim) as store:
            store.init_schema()
            # Reconcile the on-disk vector width with the model before any vector
            # work. The store owns this invariant (empty stale table → recreate;
            # populated mismatch → raise), so ingest and search behave the same.
            # Only when embedding: --no-embed writes no vectors and must not be
            # blocked by an existing index's width.
            if embedder is not None:
                store.reconcile_embedding_dim(embedder.dim)
            # --full forces re-walk even of unchanged files; default (incremental)
            # trusts files_seen mtime+size and skips matches.
            stats = ingest_directory(
                projects_root, store, force=mode_full, embedder=embedder, progress_cb=progress_cb
            )
            # FTS-at-build (C5): a --full reingest is the authoritative "make the
            # index correct" path, so rebuild the external-content FTS5 index from
            # `messages` afterwards. This heals any drift (orphaned postings, or
            # rows that predate the sync triggers) so the #22 read-time staleness
            # check becomes belt-and-suspenders rather than the only guard. The
            # incremental path relies on the triggers, which fire on every insert.
            if mode_full:
                store.rebuild_fts()
            # Self-heal: embed any messages that lack an embedding. Catches indices
            # built FTS-only or with --no-embed, which the mtime skip gate would
            # otherwise leave keyword-only forever. No-op (cheap count guard) once
            # everything is embedded.
            if embedder is not None:

                def _repair_progress(done: int) -> None:
                    click.echo(f"... {done} messages embedded (repair)", err=True)

                repaired = embed_missing(
                    store, embedder, progress_cb=_repair_progress if progress else None
                )
    except SessionStoreDimMismatchError as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)

    click.echo(
        f"files_seen={stats.files_seen} "
        f"files_ingested={stats.files_ingested} "
        f"files_skipped_unchanged={stats.files_skipped_unchanged} "
        f"messages_inserted={stats.messages_inserted} "
        f"messages_skipped_dupe={stats.messages_skipped_dupe} "
        f"messages_skipped_malformed={stats.messages_skipped_malformed} "
        f"messages_skipped_no_text={stats.messages_skipped_no_text} "
        f"messages_embedded_repaired={repaired}"
    )


if __name__ == "__main__":
    sys.exit(main())
