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

import sqlite3
import sys

import click

from rekol.config import load_config, load_rekolignore_patterns
from rekol.embeddings import get_embedder
from rekol.sessions.archive import archive_directory, backfill_once
from rekol.sessions.ingest import embed_missing, ingest_directory
from rekol.sessions.store import SessionStore, SessionStoreDimMismatchError

# NOTE: do NOT import index_write_lock here. `session-index` writes `sessions.db`,
# a separate database from the curated `index.db`; the curated `index_write_lock`
# (#24/#25) must not be reused — it would hang the SessionEnd hook behind a curated
# rebuild and couple two independent subsystems. `sessions.db` concurrency is the
# DB's own WAL + 30s `busy_timeout`; archive-sync writes idempotent flat files.
# (See the design's "Locking" section. A dedicated `.session-index.lock` is YAGNI
# for v1.)


def _sync_archive_then_pick_ingest_root(cfg, projects_root, archive_dir, progress):
    """Archive-sync the live projects dir, returning the dir ingest should read.

    When the archive is on, copy-if-changed the live transcripts into the durable
    archive and return the ARCHIVE dir, so a rebuild stays lossless even if Claude
    Code deleted the live originals (#8). On ``OSError`` (disk full, unwritable
    dir) this SOFT-FAILS: it logs a non-fatal notice and returns the LIVE projects
    dir, degrading to today's behavior — archiving must never block indexing. With
    the archive off, it simply returns the live dir.
    """
    if not cfg.archive_enabled:
        return projects_root
    exclude_patterns = list(cfg.exclude_paths) + load_rekolignore_patterns(projects_root)
    try:
        archive_stats = archive_directory(projects_root, archive_dir, exclude_patterns)
    except OSError as exc:
        # SOFT-FAIL: archiving must never block indexing. Fall back to live.
        click.echo(f"archive-sync degraded (non-fatal): {exc}; ingesting from live", err=True)
        return projects_root
    if progress:
        click.echo(
            f"... archived files_copied={archive_stats.files_copied} "
            f"files_replaced={archive_stats.files_replaced} "
            f"files_diverged_sidecar={archive_stats.files_diverged_sidecar}",
            err=True,
        )
    return archive_dir


def _backfill_once_soft_fail(cfg, archive_dir, store) -> None:
    """Run the index→archive backfill exactly once on upgrade (soft-fail).

    Delegates to the hardened, tested ``archive.backfill_once`` (the marker guard
    + before-the-run marker ordering live there) rather than duplicating that
    logic here — this is purely the CLI-layer shell: soft-fail + the one-time
    disclosure. When ``backfill_once`` returns non-``None`` with sessions
    reconstructed, emit ONE non-blocking disclosure line (what we did, where it
    lives, the off-switch).

    SOFT-FAIL: the backfill step must NEVER crash the SessionEnd hook (exit 0 is
    the contract). The old copy caught only ``OSError`` — but a corrupt/locked
    ``sessions.db`` raises ``sqlite3.DatabaseError``, which escaped and crashed the
    run. We catch ``(OSError, sqlite3.DatabaseError)`` and degrade to a logged
    notice + continue. The next successful run retries (backfill is idempotent;
    the marker is only written on a non-erroring run inside ``backfill_once``).
    """
    exclude_patterns = list(cfg.exclude_paths) + load_rekolignore_patterns(cfg.claude_projects_dir)
    try:
        stats = backfill_once(store, archive_dir, exclude_patterns)
    except (OSError, sqlite3.DatabaseError) as exc:
        click.echo(f"archive backfill degraded (non-fatal): {exc}", err=True)
        return
    if stats is not None and stats.sessions_reconstructed > 0:
        # The one-time disclosure: default-ON honesty, no per-session nag.
        click.echo(
            f"rekol built a local archive of {stats.sessions_reconstructed} past "
            f"session(s) so they're not lost — at {archive_dir}. "
            f"It's on your disk, never uploaded. Turn it off with "
            f"`archive_enabled: false` in rekol.config.yaml.",
            err=True,
        )


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

    # Determine where ingest reads FROM. With the archive on, we archive-sync the
    # live projects dir into the durable archive, then ingest from the ARCHIVE —
    # so a rebuild is lossless even if Claude Code deleted the live originals
    # (#8). If archiving soft-fails (OSError), we degrade to ingesting from live
    # (today's behavior); the next successful run catches up.
    archive_dir = cfg.archive_dir
    ingest_root = _sync_archive_then_pick_ingest_root(cfg, projects_root, archive_dir, progress)

    # NO LOCK around this block: sessions.db is a separate DB from the curated
    # index.db, so the curated index_write_lock (#24/#25) is NOT reused — it would
    # hang the SessionEnd hook behind a curated rebuild for no correctness benefit.
    # sessions.db concurrency is its own WAL + busy_timeout (set in SessionStore);
    # archive-sync above writes idempotent flat files. See the design's "Locking".
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
            # Auto-once backfill: if the archive is on and we have never
            # backfilled, reconstruct any indexed-but-unarchived sessions so
            # nothing predating the archive is lost. Marker guards re-runs. Only
            # when ingesting from the archive — a soft-failed sync (ingest_root
            # back on live) skips it, retrying next run.
            if cfg.archive_enabled and ingest_root == archive_dir:
                _backfill_once_soft_fail(cfg, archive_dir, store)
            # --full forces re-walk even of unchanged files; default (incremental)
            # trusts files_seen mtime+size and skips matches. files_seen is keyed
            # on ingest_root's paths: the FIRST run after repointing to the archive
            # re-walks once (keys differ from the old live paths), all dedupe-
            # skipped via UNIQUE(session_id, message_uuid); the mtime skip then
            # self-heals on the next run. Benign, documented in the design.
            stats = ingest_directory(
                ingest_root, store, force=mode_full, embedder=embedder, progress_cb=progress_cb
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
