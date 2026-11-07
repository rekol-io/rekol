"""Archive legacy memory files and leave a migration marker.

The archive lives under ``<memory_dir>/old-memory-archive/`` and holds the only
byte-complete copy of each original — including any ``MEMORY.md`` found in the
directory, which is MOVED here rather than deleted. Nothing in this module
unlinks a user file; if you are adding a path that does, it is wrong.

NO TIME-BASED DELETION PROMISE (#166). The marker used to say "safe to delete
after 1 week", and the prose here used to describe a one-week retention policy.
Both were false: migration synthesises a new frontmatter whitelist and
`_strip_frontmatter` discards the original YAML, so any UNRECOGNISED legacy key
survives only in the archived original. A user following that instruction
destroyed those values. Do not reintroduce a deletion deadline without a
metadata-preservation contract to back it.

We deliberately DO NOT write a ``MEMORY.md`` retirement pointer.  Claude Code's
built-in autoMemory feature auto-injects ``~/.claude/projects/<slug>/memory/MEMORY.md``
into every session — so a tombstone there is loaded as memory content, including
the literal string ``$MEMORY_HOME`` (which Claude cannot resolve in injected
text).  The hidden ``.MIGRATED`` marker has the same idempotency role for the
migrate tool but is not auto-injected.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from rekol.migrate.discover import MIGRATION_MARKER_NAME, LegacyFile

ARCHIVE_DIR_NAME = "old-memory-archive"


def archive_file(lf: LegacyFile) -> Path:
    """Move ``lf.source_path`` into ``<source_root>/old-memory-archive/``.

    Returns the new path.  If the target name already exists, appends
    ``-1``, ``-2``, ... before the suffix.
    """
    archive_dir = lf.source_root / ARCHIVE_DIR_NAME
    archive_dir.mkdir(exist_ok=True)
    target = _free_archive_name(archive_dir, lf.source_path.name)
    shutil.move(str(lf.source_path), str(target))
    return target


def _free_archive_name(archive_dir: Path, name: str) -> Path:
    """A path in ``archive_dir`` that does not exist yet: ``x.md``, ``x-1.md``, and so on.

    Never returns an existing path, so archiving can never overwrite an earlier
    archived copy — the archive holds the only byte-complete original, so one
    silent overwrite is one unrecoverable file.
    """
    target = archive_dir / name
    if not target.exists():
        return target
    stem = Path(name).stem
    suffix = Path(name).suffix
    counter = 1
    while True:
        candidate = archive_dir / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def write_retirement_pointer(memory_dir: Path, *, memory_home: Path) -> Path | None:
    """Mark ``memory_dir`` as already migrated using a hidden ``.MIGRATED`` file.

    Idempotent: if the marker already exists, leaves it alone.

    Any ``MEMORY.md`` in ``memory_dir`` is **moved into the archive**, never
    deleted. Returns the archived path, or ``None`` when there was no such file.

    WHY THIS IS A MOVE AND NOT AN UNLINK (data loss, found by QA 2026-11-06).
    This used to call ``legacy_pointer.unlink()``. The stated purpose was to
    remove a *tombstone* written by an older version of this tool, so Claude
    Code's autoMemory would stop injecting it. But both callers sit behind the
    ``has_migration_marker`` early return in ``migrator.py``, and that check
    already treats an old-format tombstone as retired — so a real tombstone is
    skipped and never reaches here. **Every file this unlink could actually
    delete was a user's own ``MEMORY.md``**, and because ``discover_files_in_dir``
    skips that name, it was neither imported nor archived first: the content
    existed nowhere afterwards. A directory holding only ``MEMORY.md`` was
    emptied and marked migrated, exit 0.
    Moving it achieves the original goal — autoMemory cannot inject what is no
    longer at that path — without the deletion the goal never required.

    The marker is written FIRST and the legacy pointer archived SECOND.
    If the process is killed mid-call, the worst case is a directory with both
    ``.MIGRATED`` and ``MEMORY.md`` — a clean re-run still skips it.  The
    opposite order would leave neither file on partial failure, causing a
    re-run to re-migrate already-archived content.
    """
    memory_dir.mkdir(parents=True, exist_ok=True)

    marker = memory_dir / MIGRATION_MARKER_NAME
    if not marker.exists():
        today = dt.date.today().isoformat()
        marker.write_text(
            f"migrated to {memory_home} on {today}\n"
            f"files discovered here are in {ARCHIVE_DIR_NAME}/ — KEEP them unless you\n"
            f"have\n"
            f"checked the migrated copies contain everything you need.\n"
            f"Migration rewrites frontmatter: recognised fields are regenerated and\n"
            f"any UNRECOGNISED key is dropped, so such values survive ONLY here.\n"
        )

    legacy_pointer = memory_dir / "MEMORY.md"
    if not legacy_pointer.exists():
        return None
    archive_dir = memory_dir / ARCHIVE_DIR_NAME
    archive_dir.mkdir(exist_ok=True)
    target = _free_archive_name(archive_dir, legacy_pointer.name)
    shutil.move(str(legacy_pointer), str(target))
    return target
