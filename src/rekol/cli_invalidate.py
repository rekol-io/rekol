"""``rekol invalidate`` — the user-facing FORGET (#64), plus memory-file invalidate.

This one leaf command is polymorphic on its single positional ``TARGET`` because
two locked call sites depend on the existing leaf surface (``cli_review`` echoes
``rekol invalidate <file>``; the curated-memory tests invoke it with a bare file
path). Rather than break those by splitting into a group, we dispatch on the
value:

* ``TARGET`` is an existing file  -> **memory-file soft-invalidate** (the
  bi-temporal model below): set ``invalidated_at`` in frontmatter and bump
  ``updated``. The file stays on disk and in the index; retrieval just
  de-prioritises it. Use ``rm`` for a hard delete. (Unchanged behaviour.)

* otherwise ``TARGET`` is a **session id** -> the **FORGET**: remove that
  session from rekol's transcript index (``sessions.db``) AND the durable
  archive, per-session (the unit both stores key on — ``session_id`` rows in the
  index, one ``.jsonl`` per session in the archive). This is what makes a
  forgotten session stay forgotten across a ``session-index`` rebuild, which
  reads the archive.

MEMORY-FILE bi-temporal rationale: a memory's facts may stop being true (a
hyperparameter changes, a service URL is decommissioned, a person leaves) but the
historical claim stays useful — "what did I believe in March?" is a real query.
So the memory branch is a SOFT state, not a delete (opt-in recall via
``rekol search --include-invalidated``; see ``ranking.py``).

HONEST CAVEAT (spec §Forget): the original transcript is **owned by Claude
Code**. Forget removes a session from *rekol* — it does NOT delete Claude Code's
live ``.jsonl`` under ``~/.claude/projects``. If that live transcript is still on
disk, a later ``session-index`` (with the archive on) re-copies it into the
archive and re-ingests it. To keep a session forgotten you must also stop it
being re-archived — either Claude Code has already rotated the live file away, or
you exclude that project (``exclude_paths`` / ``.rekolignore``). The command
states this so the user is never misled into thinking rekol deleted Claude's data.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import click
import frontmatter

from rekol.cli_common import guard_curated_schema, warn_skipped_files
from rekol.config import Config, load_config
from rekol.embeddings import get_embedder
from rekol.indexer import Indexer
from rekol.sessions.archive import load_manifest, save_manifest
from rekol.store import IndexStore

# The honest caveat, surfaced both in the forget output and (abbreviated) in help.
# Claude Code owns the live transcript; we never touch its .jsonl.
_FORGET_CAVEAT = (
    "Note: this forgets the session from rekol only. Claude Code owns the original "
    "transcript — its live .jsonl under ~/.claude/projects is NOT deleted. If that "
    "live file is still present, a later session-index re-discovers it; exclude the "
    "project (exclude_paths / .rekolignore) to keep it out for good."
)


@click.command()
@click.argument("target")
@click.option(
    "--reason",
    default="",
    help="Memory-file invalidate only: a one-line note describing why the memory "
    "is being invalidated. Appended as a body comment.",
)
@click.option(
    "--at",
    "invalidated_at",
    help="Memory-file invalidate only: ISO-8601 datetime (or date) the memory's "
    "facts stopped being true. Defaults to now.",
)
def main(target: str, reason: str, invalidated_at: str | None) -> None:
    r"""Forget TARGET from rekol.

    TARGET is dispatched by what it is:

    \b
    * an existing FILE  -> soft-invalidate that curated memory (set
      ``invalidated_at`` in frontmatter, bump ``updated``); the file stays on
      disk and in the index — use ``rm`` for a hard delete.
    * otherwise a SESSION id -> FORGET that session: remove it from rekol's
      transcript index (sessions.db) AND the durable archive, per-session.

    Forget removes a session from rekol only. Claude Code owns the original
    transcript — its live .jsonl under ~/.claude/projects is NOT deleted.
    """
    cfg = load_config()
    candidate = Path(target)
    # An existing regular file is the memory-invalidate path; anything else
    # (a non-path, a missing path, a directory) is treated as a session id.
    if candidate.is_file():
        _invalidate_memory_file(cfg, candidate, reason, invalidated_at)
        return
    _forget_session(cfg, target)


def _invalidate_memory_file(
    cfg: Config, target: Path, reason: str, invalidated_at: str | None
) -> None:
    """Soft-invalidate a curated memory file (set ``invalidated_at`` frontmatter).

    Unchanged from the original ``rekol invalidate <file>`` behaviour: the file
    stays on disk and in the index; retrieval de-prioritises it (opt-in recall via
    ``rekol search --include-invalidated``). Reindexes so the new frontmatter is
    picked up.
    """
    target = target.resolve()

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
    # across same-day invalidations is unambiguous. ``--at`` accepts either a
    # date or full datetime string and is passed through unchanged.
    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    today_date = dt.date.today().isoformat()
    post.metadata["invalidated_at"] = invalidated_at or now_iso
    post.metadata["updated"] = now_iso

    if reason:
        post.content = post.content.rstrip() + f"\n\n<!-- invalidated {today_date}: {reason} -->\n"

    target.write_text(frontmatter.dumps(post) + "\n")
    click.echo(f"invalidated {target} (at={post.metadata['invalidated_at']})")

    # Reindex so the new frontmatter is picked up. The body changed if a reason
    # was supplied, so the indexer would re-embed anyway.
    embedder = get_embedder(cfg.embedding_model)
    store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
    store.init_schema()
    guard_curated_schema(store)
    idx = Indexer(
        memory_root=cfg.memory_home,
        store=store,
        embedder=embedder,
        chunk_max_bytes=cfg.chunk_max_bytes,
        index_dir=cfg.index_dir,
    )
    stats = idx.update()
    warn_skipped_files(stats)


def _delete_session_from_index(sessions_db_path: Path, session_id: str) -> int:
    """Delete every message row for ``session_id`` from ``sessions.db``.

    Returns the number of message rows removed. Mirrors what the store's own
    triggers and tables require for a CLEAN removal — we cannot leave dangling
    state that a later read would trip over:

    * ``messages`` rows are deleted directly; the ``messages_ad`` AFTER DELETE
      trigger keeps the external-content FTS5 index in sync (no orphaned
      postings), so keyword search forgets the session too.
    * the vector index has NO delete trigger, so we delete the matching
      ``messages_vec`` / ``messages_vec_numpy`` rows BY ROWID first (while the
      ``messages`` rows still exist to resolve the rowids) — otherwise the
      embeddings would orphan and semantic search could still surface the
      forgotten turns.
    * the ``files_seen`` mtime/size skip row for the session's archived path is
      cleared so a re-index does not falsely "skip unchanged" a file that no
      longer holds this session.

    A missing DB (session search never ran) means nothing to delete here — that
    is a 0, not an error; the caller decides whether the session was found in
    EITHER store. Opens with a short busy_timeout so a concurrent SessionEnd
    write does not fail the delete outright.
    """
    if not sessions_db_path.is_file():
        return 0
    conn = sqlite3.connect(sessions_db_path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000;")
        # ``messages_vec`` is a sqlite-vec vec0 virtual table; a plain connection
        # cannot DELETE from it ("no such module: vec0") unless the extension is
        # loaded. Mirror SessionStore's loader so the embedding rows are removed
        # too — without this the forgotten turns would orphan in the vector index
        # and semantic search could still surface them.
        _load_vec_extension(conn)
        rowids = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM messages WHERE session_id = ?", (session_id,)
            ).fetchall()
        ]
        if not rowids:
            return 0
        archived_paths = {
            r["jsonl_path"]
            for r in conn.execute(
                "SELECT DISTINCT jsonl_path FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            if r["jsonl_path"]
        }
        with conn:
            # Drop embeddings first, by rowid, while messages still resolve them.
            # Both possible vector tables are tried; only one exists per DB, and a
            # missing one is a benign no-op (the table simply isn't there).
            for vec_table in ("messages_vec", "messages_vec_numpy"):
                if _table_exists(conn, vec_table):
                    conn.executemany(
                        f"DELETE FROM {vec_table} WHERE rowid = ?",
                        [(rowid,) for rowid in rowids],
                    )
            # Deleting the messages fires messages_ad, which removes the FTS
            # postings — so keyword search forgets the session as well.
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            # Clear the mtime/size skip row for each archived path this session
            # was ingested from, so a re-index never falsely skips it as unchanged.
            for jsonl_path in archived_paths:
                conn.execute("DELETE FROM files_seen WHERE jsonl_path = ?", (jsonl_path,))
        return len(rowids)
    finally:
        conn.close()


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    """Best-effort load of the sqlite-vec extension onto ``conn``.

    The vector index is a vec0 virtual table; deleting from it requires the
    extension. Mirrors ``SessionStore._try_load_vec`` deliberately (we cannot
    reuse that private method without reopening the DB through the store): a
    missing package or a loader that fails leaves the connection on the numpy
    fallback table (``messages_vec_numpy``), which is a plain table that needs no
    extension — so the delete still works. We swallow the load error here (rather
    than warn) because the only consumer is a one-shot delete, and the
    table-existence check downstream picks the right table regardless.
    """
    try:
        import sqlite_vec
    except ImportError:
        return
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (sqlite3.OperationalError, AttributeError):
        # Python built without SQLITE_ENABLE_LOAD_EXTENSION, or the loader is
        # unavailable. The numpy-fallback DB needs no extension; a vec0 DB on such
        # a Python could not have been written in the first place, so there are no
        # vec0 rows to delete here.
        return


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True when ``name`` is a table (incl. virtual table) in the open DB."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _delete_session_from_archive(archive_dir: Path, session_id: str) -> int:
    """Remove archived ``.jsonl`` files holding ``session_id`` + their manifest rows.

    Returns the number of archive files removed. The archive stores one canonical
    ``.jsonl`` per session (plus optional divergence sidecars), named by the
    session id — but a backfill-reconstructed file or a sidecar could be named
    differently, so we do NOT trust the filename alone: we read each archived
    file's first non-empty row and match its ``sessionId``. This is the same unit
    the archive reconciles on, so removing the file (and dropping its manifest
    entry) means the next archive-sync treats it as genuinely absent rather than a
    false "unchanged" skip — and a re-index reading the archive cannot resurrect it.

    A non-existent archive dir is a 0 (nothing archived yet), not an error.
    """
    if not archive_dir.is_dir():
        return 0
    manifest = load_manifest(archive_dir)
    removed = 0
    manifest_dirty = False
    for archived_path in sorted(archive_dir.glob("**/*.jsonl")):
        if not _archived_file_is_session(archived_path, session_id):
            continue
        try:
            archived_path.unlink()
        except FileNotFoundError:
            # Already gone (concurrent prune / double-run) — treat as removed.
            pass
        except OSError as exc:
            raise click.ClickException(
                f"could not remove archived transcript {archived_path}: {exc}"
            ) from exc
        removed += 1
        # Drop the manifest entry (keyed by the archive-relative path) so a later
        # sync does not see a stale "unchanged" record for a file that is gone.
        manifest_key = str(archived_path.relative_to(archive_dir))
        if manifest.pop(manifest_key, None) is not None:
            manifest_dirty = True
    if manifest_dirty:
        save_manifest(archive_dir, manifest)
    return removed


def _archived_file_is_session(archived_path: Path, session_id: str) -> bool:
    """True when an archived ``.jsonl``'s first parseable row carries ``session_id``.

    Reads only until the first row with a ``sessionId`` (it is on row 1 in
    practice; every reconstructed/live row carries it), so we never slurp a large
    transcript just to identify it. Reads with ``errors="replace"`` so a malformed
    byte cannot crash the scan; an unreadable file returns False (fail-safe: we do
    not delete a file we cannot positively identify as the target session).
    """
    try:
        with archived_path.open(encoding="utf-8", errors="replace") as handle:
            for line_number, raw_line in enumerate(handle):
                if line_number >= 50:  # sessionId is on the first row in practice
                    break
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                sid = row.get("sessionId")
                if isinstance(sid, str) and sid:
                    return sid == session_id
    except OSError:
        return False
    return False


def _forget_session(cfg: Config, session_id: str) -> None:
    """FORGET a session: remove it from the transcript index AND the durable archive.

    Per-session — the unit both stores key on. Errors LOUDLY (non-zero exit) when
    the session is in NEITHER store, so a typo'd id is never a silent no-op that
    leaves the user believing they forgot something they did not. Always prints the
    honest caveat: forget is rekol-side only; Claude Code's live .jsonl is untouched.
    """
    index_rows = _delete_session_from_index(cfg.sessions_db_path, session_id)
    archive_files = _delete_session_from_archive(cfg.archive_dir, session_id)

    if index_rows == 0 and archive_files == 0:
        raise click.ClickException(
            f"session {session_id!r} not found in rekol's index or archive — "
            f"nothing to forget (check the session id)."
        )

    click.echo(
        f"forgot session {session_id} "
        f"(index_rows_removed={index_rows} archive_files_removed={archive_files})"
    )
    click.echo(_FORGET_CAVEAT)


if __name__ == "__main__":
    sys.exit(main())
