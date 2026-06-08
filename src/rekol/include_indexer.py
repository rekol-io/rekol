"""Ongoing indexing of include_dirs into synthetic transcripts (T8 #63 scope 3).

This is the "extend the reindex hook beyond ``$REKOL_HOME``" half of include-
scope. On every ``session-index`` run we convert each in-scope directory into
synthetic Claude Code JSONL under ``claude_projects_dir/<prefix>/``, so the
EXISTING transcript ingester (no changes to it) picks the content up and it
becomes searchable. A dir included once therefore auto-indexes its new files
forever, governed by the deny-list — no filewatcher.

INCREMENTAL: a per-dir MANIFEST (the sorted ``(rel-path, mtime, size)`` of every
in-scope file) is hashed and cached under the local index dir. If the manifest
is byte-identical to the last run's, the directory is SKIPPED — no re-walk, no
re-convert, no rewrite — so the steady-state cost is one cheap scan per included
dir. A new/removed/edited in-scope file changes the manifest and triggers a
re-convert. The deny-list and the always-on junk filter both feed the manifest,
so denying a folder both drops it from conversion AND, by changing the hash,
forces the one rebuild that removes its stale transcripts.

We REUSE ``docs_convert`` primitives (``group_sessions`` / ``extract_text`` /
``build_rows`` / ``write_sessions``) rather than re-implementing conversion; the
only thing this module adds is the deny/junk scope filter over the grouped files
and the incremental manifest.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from rekol.config import Config
from rekol.docs_convert.extract import extract_text
from rekol.docs_convert.transcript import build_rows
from rekol.docs_convert.walk import SessionGroup, group_sessions
from rekol.docs_convert.writer import write_sessions
from rekol.include_scope import file_in_scope

# Backstop against one pathological file becoming one giant low-precision FTS
# row — same default the one-time ``import`` used.
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB

# Marker subdir under the local index cache where per-dir manifest hashes live.
# Lives in the cache (NOT $REKOL_HOME) like every other derived state, so it is
# never synced and is safe to delete (a missing marker just forces one rebuild).
_MANIFEST_SUBDIR = "include-scope"


@dataclass
class IncludeIndexResult:
    """Outcome of one ``index_include_dirs`` run, for the CLI to report.

    ``dirs_converted`` and ``dirs_skipped_unchanged`` are RESOLVED dir paths so
    callers can correlate with the config. ``files_converted`` is the total rows
    written this run (0 when everything skipped). ``dirs_missing`` records
    configured dirs that do not exist on disk (reported, never fatal).
    """

    dirs_converted: list[Path] = field(default_factory=list)
    dirs_skipped_unchanged: list[Path] = field(default_factory=list)
    dirs_missing: list[Path] = field(default_factory=list)
    files_converted: int = 0

    def as_line(self) -> str:
        """Render the run as a single space-separated key=value log line."""
        return (
            f"include_dirs_converted={len(self.dirs_converted)} "
            f"include_dirs_skipped_unchanged={len(self.dirs_skipped_unchanged)} "
            f"include_dirs_missing={len(self.dirs_missing)} "
            f"include_files_converted={self.files_converted}"
        )


def include_prefix_for(include_dir: Path) -> str:
    """Return the stable, collision-free transcript prefix for an included dir.

    Derived from a sha1 of the RESOLVED absolute path, so the same dir always
    maps to the same prefix (incremental + idempotent re-ingest) and two
    different dirs can never collide. The ``rekol-include-`` prefix makes these
    synthetic-transcript folders recognisable under ``claude_projects_dir`` and
    greppable for cleanup.
    """
    digest = hashlib.sha1(str(include_dir.resolve()).encode()).hexdigest()[:16]
    return f"rekol-include-{digest}"


def _scoped_groups(include_dir: Path, deny: list[str]) -> list[SessionGroup]:
    """Group the dir into sessions, then drop every out-of-scope file.

    Reuses ``group_sessions`` for the dir->session mapping, then filters each
    group's ``files`` through :func:`file_in_scope` (deny-list + junk filter).
    Groups left empty after filtering are dropped so no empty ``.jsonl`` is
    written.
    """
    groups = group_sessions(include_dir)
    scoped: list[SessionGroup] = []
    for group in groups:
        kept = [entry for entry in group.files if file_in_scope(entry.path, include_dir, deny)]
        if not kept:
            continue
        scoped.append(
            SessionGroup(
                session_name=group.session_name,
                folder_abs=group.folder_abs,
                rel_to_source=group.rel_to_source,
                files=kept,
            )
        )
    return scoped


def _manifest_hash(groups: list[SessionGroup]) -> str:
    """Hash the in-scope fileset's identity (rel-path, mtime, size) for skip-detection.

    Deterministic: every entry is sorted by ``rel_to_source`` and serialised with
    its mtime+size, so an edit (size/mtime change), an add, or a removal all
    change the hash, while an untouched tree hashes identically run-to-run. The
    deny-list affects which files are in ``groups``, so a deny change also moves
    the hash — forcing the rebuild that purges the now-out-of-scope transcripts.
    """
    lines: list[str] = []
    for group in groups:
        for entry in sorted(group.files, key=lambda e: e.rel_to_source):
            try:
                stat = entry.path.stat()
            except OSError:
                # A file that vanished between scan and hash: encode a sentinel so
                # the manifest changes (forcing a rebuild) rather than silently
                # matching the prior hash.
                lines.append(f"{entry.rel_to_source}\tMISSING")
                continue
            lines.append(f"{entry.rel_to_source}\t{int(stat.st_mtime)}\t{int(stat.st_size)}")
    payload = "\n".join(lines)
    return hashlib.sha256(payload.encode()).hexdigest()


def _marker_path(cfg: Config, prefix: str) -> Path:
    """Path to the cached manifest-hash marker for a given include prefix."""
    return cfg.index_dir / _MANIFEST_SUBDIR / f"{prefix}.manifest"


def _read_marker(marker: Path) -> str | None:
    """Read a stored manifest hash, or None when absent/unreadable (force rebuild)."""
    if not marker.is_file():
        return None
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_marker(marker: Path, manifest_hash: str) -> None:
    """Persist the manifest hash for next-run skip-detection (best-effort).

    Written AFTER a successful convert+write so a crash mid-convert leaves the old
    (or no) marker and the next run re-converts. An ``OSError`` here is swallowed:
    a failed marker write only costs one redundant re-convert next run, never a
    crash of the SessionEnd hook.
    """
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(manifest_hash, encoding="utf-8")
    except OSError:
        return


def _convert_one_dir(
    include_dir: Path,
    deny: list[str],
    target_dir: Path,
    max_bytes: int,
) -> int:
    """Convert one in-scope dir to synthetic transcripts. Returns rows written.

    Mirrors ``docs_convert.convert_tree`` but over the SCOPED (deny/junk-filtered)
    groups. ``write_sessions`` rmtree+recreates the prefix dir, so a removed or
    now-denied file leaves no stale ``.jsonl`` behind.
    """
    groups = _scoped_groups(include_dir, deny)
    prefix = include_prefix_for(include_dir)
    rows_by_session: dict[str, list[dict]] = {}
    for group in groups:
        texts: dict[str, str] = {}
        for entry in group.files:
            try:
                text = extract_text(entry.path, max_bytes=max_bytes)
            except OSError:
                continue
            if text is None:
                continue
            texts[entry.rel_to_session] = text
        rows = build_rows(group, prefix=prefix, texts=texts)
        if rows:
            rows_by_session[group.session_name] = rows
    write_sessions(target_dir, prefix=prefix, rows_by_session=rows_by_session)
    return sum(len(rows) for rows in rows_by_session.values())


def index_include_dirs(cfg: Config, max_bytes: int = _DEFAULT_MAX_BYTES) -> IncludeIndexResult:
    """Convert every in-scope include dir into synthetic transcripts, incrementally.

    For each resolved ``include_dirs`` entry:
      * missing dir -> recorded in ``dirs_missing`` and skipped (never fatal),
      * unchanged since last run (manifest hash matches the cached marker) ->
        recorded in ``dirs_skipped_unchanged`` and skipped (no re-convert),
      * otherwise -> converted to synthetic transcripts under
        ``claude_projects_dir/<prefix>/`` and the marker is refreshed.

    The synthetic transcripts are then ingested by the SAME ``session-index``
    pass that calls this (the caller ingests ``claude_projects_dir`` afterwards),
    so no separate ingest step is needed. Returns an :class:`IncludeIndexResult`
    the CLI prints.
    """
    result = IncludeIndexResult()
    deny = list(cfg.include_deny)
    target_dir = cfg.claude_projects_dir
    for include_dir in cfg.resolved_include_dirs():
        if not include_dir.is_dir():
            result.dirs_missing.append(include_dir)
            continue
        prefix = include_prefix_for(include_dir)
        marker = _marker_path(cfg, prefix)
        groups = _scoped_groups(include_dir, deny)
        manifest_hash = _manifest_hash(groups)
        if _read_marker(marker) == manifest_hash:
            result.dirs_skipped_unchanged.append(include_dir)
            continue
        # Changed (or first time): rebuild the prefix and refresh the marker.
        target_dir.mkdir(parents=True, exist_ok=True)
        written = _convert_one_dir(include_dir, deny, target_dir, max_bytes)
        _write_marker(marker, manifest_hash)
        result.dirs_converted.append(include_dir)
        result.files_converted += written
    return result


def dir_is_current(cfg: Config, include_dir: Path) -> bool:
    """True when ``include_dir``'s in-scope fileset matches its cached marker.

    The coverage report uses this to decide whether a dir's files are reflected
    in the index: a dir whose current scoped-manifest hash equals its stored
    marker has been converted with exactly this fileset (so its transcripts are
    in the index), whereas a new/edited/removed file (or a deny change) moves the
    hash and the dir is "pending re-index". Pure read — no conversion, no marker
    write. A missing dir is not current (there is nothing indexed for it).
    """
    if not include_dir.is_dir():
        return False
    groups = _scoped_groups(include_dir, list(cfg.include_deny))
    manifest_hash = _manifest_hash(groups)
    return _read_marker(_marker_path(cfg, include_prefix_for(include_dir))) == manifest_hash


def scoped_file_count(cfg: Config, include_dir: Path) -> int:
    """Count the in-scope (deny/junk-filtered) files under ``include_dir``.

    The "discoverable" unit for one dir. A missing dir contributes 0. Counts the
    same grouped+filtered fileset that conversion would convert, so coverage's
    discoverable total never disagrees with what indexing would actually take.
    """
    if not include_dir.is_dir():
        return 0
    return sum(len(group.files) for group in _scoped_groups(include_dir, list(cfg.include_deny)))


__all__ = [
    "IncludeIndexResult",
    "dir_is_current",
    "include_prefix_for",
    "index_include_dirs",
    "scoped_file_count",
]
