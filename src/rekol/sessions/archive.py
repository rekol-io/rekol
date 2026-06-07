"""Durable, rekol-owned transcript archive sink between live transcripts and the index.

The sink sits between Claude Code's ephemeral ``~/.claude/projects/**/*.jsonl``
and the disposable ``sessions.db``.

DB-FREE BY DESIGN: this module is pure filesystem + a JSON manifest. It owns the
copy-if-changed primitive (with a divergence sidecar for the compaction/rewrite
case), the directory reconcile (copy new, skip unchanged, remove now-excluded),
the index->archive backfill, and the manual prune. Keeping it DB-free means the
archive can be rebuilt or inspected with no SQLite dependency, and a bug here can
never corrupt the index.

SOFT-FAIL DISCIPLINE: every public entry point that touches the filesystem is
written so an ``OSError`` (disk full, dir unwritable, permission) surfaces as a
caught, logged degradation — never an uncaught crash — because archiving must
never block indexing (see cli_session_index).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from rekol.config import path_is_excluded

# Manifest file name (hidden, inside the archive root). Maps a live-relative
# path -> {"mtime_unix": int, "size_bytes": int} recorded at last archive.
MANIFEST_FILENAME = ".manifest.json"
# One-time backfill guard marker; presence means "we already backfilled from the
# index on upgrade", so the auto-once backfill never re-runs.
BACKFILL_MARKER_FILENAME = ".backfilled-from-index"


def load_manifest(archive_dir: Path) -> dict[str, dict[str, int]]:
    """Read the archive manifest, or ``{}`` when absent/corrupt.

    A missing or corrupt manifest degrades to an empty mapping rather than
    raising: copy-if-changed is idempotent, so the worst case is re-archiving a
    file that was already current. We never let a bad manifest crash a sync.
    """
    manifest_path = archive_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return {}
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        loaded = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        # Corrupt/unreadable manifest -> treat as empty (re-archive everything).
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def save_manifest(archive_dir: Path, manifest: dict[str, dict[str, int]]) -> None:
    """Atomically write the archive manifest.

    Writes to a temp file in the same dir then ``os.replace``s it, so a crash
    mid-write can never leave a half-written manifest (which would otherwise read
    back as corrupt and force a full re-archive).
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = archive_dir / MANIFEST_FILENAME
    tmp_path = manifest_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(manifest_path)


@dataclass
class ArchiveFileResult:
    """Outcome of archiving one live file.

    ``action`` is one of: ``copied`` (no prior archive), ``skipped_unchanged``
    (manifest mtime+size match), ``replaced_append`` (live grew, archived was a
    true prefix), ``diverged_sidecar`` (live shorter/diverged — kept both copies
    via a sidecar). ``sidecar_path`` is set only for ``diverged_sidecar``.
    """

    action: str
    sidecar_path: Path | None = None


def _archived_is_prefix_of_live(archived_path: Path, live_path: Path) -> bool:
    """True when the archived file's bytes are a true prefix of the live file's.

    This is the "normal append" signature: Claude Code appended rows to the
    session and the archived copy is the earlier, shorter version. We compare
    bytes (not text) so an encoding quirk can never misclassify. Read errors
    return False (treat as divergence -> keep both copies; the safe direction).
    """
    try:
        archived_bytes = archived_path.read_bytes()
        live_bytes = live_path.read_bytes()
    except OSError:
        return False
    if len(archived_bytes) > len(live_bytes):
        return False
    return live_bytes[: len(archived_bytes)] == archived_bytes


def _divergence_sidecar_path(archived_path: Path, live_path: Path) -> Path:
    """Path for the divergence sidecar: ``<stem>.<shorthash>.jsonl``.

    The shorthash is the first 8 hex chars of the SHA-256 of the live content,
    so the SAME divergent content always maps to the SAME sidecar path —
    re-running a sync is idempotent and never piles up duplicates. (DB-level
    uuid dedupe folds the two copies at ingest, so a duplicate sidecar would be
    harmless but messy.)
    """
    digest = hashlib.sha256(live_path.read_bytes()).hexdigest()[:8]
    # archived_path is e.g. <dir>/<session-id>.jsonl; insert the shorthash before
    # the .jsonl suffix -> <dir>/<session-id>.<shorthash>.jsonl.
    return archived_path.with_suffix("").with_suffix(f".{digest}.jsonl")


def archive_file(
    live_path: Path,
    archived_path: Path,
    manifest: dict[str, dict[str, int]],
    *,
    manifest_key: str,
) -> ArchiveFileResult:
    """Copy-if-changed primitive: reconcile one live file into the archive.

    Mutates ``manifest[manifest_key]`` in place on copy/replace (caller persists
    it once per directory). The five cases (see module docstring + design):

    * no archived copy             -> copy, record manifest
    * unchanged (mtime+size match)  -> skip
    * live grew, archived is prefix -> replace (normal append), record manifest
    * live shorter / not a prefix   -> keep archive, write divergence sidecar

    Raises ``OSError`` to the caller (``archive_directory``), which counts it and
    keeps going — one unreadable file must not abort the whole sync.
    """
    live_stat = live_path.stat()
    live_mtime = int(live_stat.st_mtime)
    live_size = int(live_stat.st_size)

    if not archived_path.exists():
        archived_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live_path, archived_path)
        manifest[manifest_key] = {"mtime_unix": live_mtime, "size_bytes": live_size}
        return ArchiveFileResult(action="copied")

    recorded = manifest.get(manifest_key)
    if (
        recorded is not None
        and recorded.get("mtime_unix") == live_mtime
        and recorded.get("size_bytes") == live_size
    ):
        # Steady-state cheap path: nothing changed since we last archived it.
        return ArchiveFileResult(action="skipped_unchanged")

    if _archived_is_prefix_of_live(archived_path, live_path):
        # Normal append: the archived copy is an earlier prefix; replace wholesale.
        shutil.copy2(live_path, archived_path)
        manifest[manifest_key] = {"mtime_unix": live_mtime, "size_bytes": live_size}
        return ArchiveFileResult(action="replaced_append")

    # Divergence (compaction/rewrite): live is shorter or not a prefix. NEVER
    # overwrite — keep the existing archive and write the new version beside it.
    sidecar_path = _divergence_sidecar_path(archived_path, live_path)
    if not sidecar_path.exists():
        shutil.copy2(live_path, sidecar_path)
    # Intentionally do NOT update the manifest key here: the canonical archived
    # file is unchanged, and the sidecar is found by ingest's directory glob.
    return ArchiveFileResult(action="diverged_sidecar", sidecar_path=sidecar_path)


@dataclass
class ArchiveStats:
    """Tally of an ``archive_directory`` reconcile run."""

    files_seen: int = 0
    files_copied: int = 0
    files_replaced: int = 0
    files_skipped_unchanged: int = 0
    files_skipped_excluded: int = 0  # live file matched an exclude -> never archived
    files_diverged_sidecar: int = 0  # compaction/rewrite -> kept both copies
    files_removed_excluded: int = 0  # already-archived file now matches an exclude -> rm'd
    files_errored: int = 0  # OSError on one file; counted, sync continues


def _read_cwd_from_jsonl(jsonl_path: Path) -> str | None:
    """Read the session's REAL cwd from the first message row that carries one.

    The exclude matcher must run against the project path the user actually worked
    in (e.g. ``/Users/x/secret``), NOT Claude Code's URL-encoded folder slug
    (``-Users-x-secret``) — otherwise a natural pattern like ``*/secret/*`` would
    silently never match the on-disk path. Every Claude Code transcript row carries
    a ``cwd`` field, so we scan from the top and return the first non-empty one.

    Reads only the first few lines (the cwd is on row 1 in practice; we cap the scan
    so a huge transcript is not slurped just to find it). Returns ``None`` when the
    file is unreadable or no row has a cwd — the caller then does NOT exclude it
    (fail-open: a missing cwd must not silently drop a session from the archive).
    """
    try:
        with jsonl_path.open(encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle):
                if line_number >= 50:  # cwd is on the first row in practice
                    break
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                cwd = row.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _remove_now_excluded_from_archive(
    archive_dir: Path,
    exclude_patterns: list[str],
    manifest: dict[str, dict[str, int]],
    stats: ArchiveStats,
) -> None:
    """Placeholder — real retroactive-removal logic lands in Task 3.2."""
    return


def archive_directory(
    live_root: Path,
    archive_dir: Path,
    exclude_patterns: list[str],
) -> ArchiveStats:
    """Reconcile the archive to ``(live ∩ not-excluded)``.

    Walks every ``*.jsonl`` under ``live_root`` (typically
    ``~/.claude/projects``), and for each non-excluded file runs the
    copy-if-changed primitive (:func:`archive_file`). Then it sweeps the archive
    for files that now match an exclude and removes them (the safe retroactive
    flat-file delete — see exclude slice).

    EXCLUDE MATCHES THE REAL cwd, NOT the slug. The forward skip reads the first
    message row's ``cwd`` from each ``.jsonl`` (the project path the user actually
    worked in, e.g. ``/Users/x/secret``) and matches ``exclude_patterns`` against
    THAT — never against Claude Code's URL-encoded folder name
    (``-Users-x-secret``), which would make a natural pattern like ``*/secret/*``
    silently never match. This is symmetric with the retroactive removal and the
    backfill, which also match the real cwd. ``.rekolignore`` patterns are merged
    in by the caller (``cli_archive`` / ``cli_session_index``) before calling this,
    so this function takes a single already-combined pattern list.

    SOFT-FAIL: an ``OSError`` on one file is caught, counted in
    ``files_errored``, and the walk continues — one bad file never aborts the
    sync. The manifest is persisted once at the end (after all copies).

    Returns the :class:`ArchiveStats` tally.
    """
    stats = ArchiveStats()
    manifest = load_manifest(archive_dir)
    live_root = Path(live_root)

    for live_path in sorted(live_root.glob("**/*.jsonl")):
        stats.files_seen += 1
        # manifest_key / archive layout mirror the live tree relative to root.
        relative = live_path.relative_to(live_root)
        manifest_key = str(relative)
        # Exclude on the session's REAL cwd (read from the file), NOT the slug-
        # encoded path on disk — so a pattern like `*/secret/*` matches the project
        # the user worked in. A file with no readable cwd is never excluded here
        # (it falls through to archiving; an unreadable file is caught below). Only
        # read the cwd when there ARE excludes — otherwise every sync would pay a
        # per-file read for nothing (the common no-exclude case stays cheap).
        if exclude_patterns:
            session_cwd = _read_cwd_from_jsonl(live_path)
            if session_cwd and path_is_excluded(session_cwd, exclude_patterns):
                stats.files_skipped_excluded += 1
                continue
        archived_path = archive_dir / relative
        try:
            result = archive_file(live_path, archived_path, manifest, manifest_key=manifest_key)
        except OSError:
            # One unreadable/unwritable file must not abort the run; the next
            # successful sync catches it (copy-if-changed is idempotent).
            stats.files_errored += 1
            continue
        if result.action == "copied":
            stats.files_copied += 1
        elif result.action == "replaced_append":
            stats.files_replaced += 1
        elif result.action == "skipped_unchanged":
            stats.files_skipped_unchanged += 1
        elif result.action == "diverged_sidecar":
            stats.files_diverged_sidecar += 1

    _remove_now_excluded_from_archive(archive_dir, exclude_patterns, manifest, stats)
    save_manifest(archive_dir, manifest)
    return stats
