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

import json
from pathlib import Path

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
