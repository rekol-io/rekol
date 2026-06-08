"""rekol archive: manual transcript-archive operations.

Default (no flags)   sync the durable archive from ~/.claude/projects
--from-index         reconstruct archive files for sessions present in the
                     index but missing from the archive (the upgrade backfill;
                     also auto-run once by session-index — see that module)
--prune --clear      flat-file removal of the whole archive (manual retention)

SOFT-FAIL: archiving never blocks anything; an OSError degrades to a logged
warning and exit 0 (the SessionEnd-hook contract — a broken archive step must
not stall a session).

LOCKING: deliberately none. This command touches the durable archive (flat
files) and reads ``sessions.db``; it NEVER touches the curated ``index.db``, so
it must not take the curated ``index_write_lock`` (#24/#25) — doing so would hang
the SessionEnd hook behind a curated rebuild and couple two independent
subsystems. ``sessions.db`` concurrency is the DB's own WAL + 30s busy_timeout;
the flat-file reconcile is idempotent. See the design's "Locking" section.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from rekol.config import Config, load_config, load_rekolignore_patterns
from rekol.sessions.archive import (
    BACKFILL_MARKER_FILENAME,
    archive_directory,
    backfill_from_index,
    prune,
)
from rekol.sessions.store import SessionStore


@click.command()
@click.option(
    "--from-index",
    "from_index",
    is_flag=True,
    help="Reconstruct archive files for sessions in the index but missing from "
    "the archive (text-only/lossy). The upgrade backfill; safe to re-run.",
)
@click.option(
    "--prune",
    "do_prune",
    is_flag=True,
    help="Manual retention. With --clear, removes every archived transcript.",
)
@click.option(
    "--clear",
    "do_clear",
    is_flag=True,
    help="With --prune: remove ALL archived transcripts and reset the manifest.",
)
def main(from_index: bool, do_prune: bool, do_clear: bool) -> None:
    """Sync, backfill, or prune the durable transcript archive."""
    cfg = load_config()
    archive_dir = cfg.archive_dir

    if not cfg.archive_enabled:
        # The off-switch: report and exit cleanly so scripts/tests can detect it.
        click.echo("archive_enabled=false in config; nothing to do.")
        sys.exit(0)

    # Combine config excludes with any per-folder .rekolignore at the projects
    # root, so a sensitive project is never archived from either source.
    exclude_patterns = list(cfg.exclude_paths) + load_rekolignore_patterns(cfg.claude_projects_dir)

    # NO LOCK: this writes flat files (and reads sessions.db), never the curated
    # index.db, so the curated index_write_lock must not be reused here (it would
    # hang the SessionEnd hook behind a curated rebuild). The flat-file reconcile
    # is idempotent and sessions.db has its own WAL + busy_timeout.
    try:
        if do_prune:
            prune_stats = prune(archive_dir, clear=do_clear)
            click.echo(f"pruned files_removed={prune_stats.files_removed}")
        elif from_index:
            _run_backfill(cfg, archive_dir, exclude_patterns)
        else:
            stats = archive_directory(cfg.claude_projects_dir, archive_dir, exclude_patterns)
            click.echo(
                f"files_seen={stats.files_seen} "
                f"files_copied={stats.files_copied} "
                f"files_replaced={stats.files_replaced} "
                f"files_skipped_unchanged={stats.files_skipped_unchanged} "
                f"files_skipped_excluded={stats.files_skipped_excluded} "
                f"files_diverged_sidecar={stats.files_diverged_sidecar} "
                f"files_removed_excluded={stats.files_removed_excluded} "
                f"files_errored={stats.files_errored}"
            )
    except OSError as exc:
        # Soft-fail: never block on a filesystem error (disk full, unwritable).
        click.echo(f"archive operation degraded (non-fatal): {exc}", err=True)
        sys.exit(0)


def _run_backfill(cfg: Config, archive_dir: Path, exclude_patterns: list[str]) -> None:
    """Run the index->archive backfill and write the one-time marker + notice."""
    sessions_db = cfg.sessions_db_path
    if not sessions_db.exists():
        click.echo("no sessions.db to backfill from; nothing to do.")
        return
    # Write the one-time guard marker BEFORE running the backfill. backfill is
    # idempotent (it skips sessions already present), so the marker firing first
    # means a crash MID-backfill still leaves the marker set — the auto-once notice
    # then fires exactly once and the next normal sync (or an explicit
    # `rekol archive --from-index`) finishes any leftover work. Writing the marker
    # only on success would re-run the whole backfill (and re-emit the notice) on
    # every run until one completes uninterrupted.
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / BACKFILL_MARKER_FILENAME).touch()
    with SessionStore(db_path=sessions_db, dim=384) as store:
        store.init_schema()
        stats = backfill_from_index(store, archive_dir, exclude_patterns)
    click.echo(
        f"backfill sessions_reconstructed={stats.sessions_reconstructed} "
        f"sessions_skipped_present={stats.sessions_skipped_present} "
        f"sessions_skipped_excluded={stats.sessions_skipped_excluded} "
        f"sessions_errored={stats.sessions_errored}"
    )


if __name__ == "__main__":
    sys.exit(main())
