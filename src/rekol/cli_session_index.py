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
from rekol.sessions.ingest import ingest_directory
from rekol.sessions.store import SessionStore


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
    default=False,
    show_default=True,
    help="Compute vector embeddings for new messages. Off by default for speed; "
    "Phase 2 will turn this on once search wiring is in place. (v1 stub — "
    "the flag is accepted but no embedding work is performed yet.)",
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

    # Progress callback prints to stderr so it doesn't interleave with the
    # final stats line on stdout (tests assert against stdout substrings).
    progress_cb = None
    if progress:

        def _emit_progress(done: int, total: int) -> None:
            click.echo(f"... {done}/{total} files indexed", err=True)

        progress_cb = _emit_progress

    with SessionStore(db_path=cfg.sessions_db_path, dim=384) as store:
        store.init_schema()
        # --full forces re-walk even of unchanged files; default (incremental)
        # trusts files_seen mtime+size and skips matches.
        stats = ingest_directory(projects_root, store, force=mode_full, progress_cb=progress_cb)

    click.echo(
        f"files_seen={stats.files_seen} "
        f"files_ingested={stats.files_ingested} "
        f"files_skipped_unchanged={stats.files_skipped_unchanged} "
        f"messages_inserted={stats.messages_inserted} "
        f"messages_skipped_dupe={stats.messages_skipped_dupe} "
        f"messages_skipped_malformed={stats.messages_skipped_malformed} "
        f"messages_skipped_no_text={stats.messages_skipped_no_text}"
    )


if __name__ == "__main__":
    sys.exit(main())
