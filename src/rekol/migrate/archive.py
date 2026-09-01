"""Archive legacy memory files and leave a migration marker.

The archive lives under ``<memory_dir>/old-memory-archive/`` and holds the only
byte-complete copy of each original.

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

    target = archive_dir / lf.source_path.name
    counter = 1
    stem = lf.source_path.stem
    suffix = lf.source_path.suffix
    while target.exists():
        target = archive_dir / f"{stem}-{counter}{suffix}"
        counter += 1

    shutil.move(str(lf.source_path), str(target))
    return target


def write_retirement_pointer(memory_dir: Path, *, memory_home: Path) -> None:
    """Mark ``memory_dir`` as already migrated using a hidden ``.MIGRATED`` file.

    Idempotent: if the marker already exists, leaves it alone.  Also removes
    any pre-existing ``MEMORY.md`` left over from prior versions of this tool —
    those would otherwise be auto-injected by Claude Code's built-in autoMemory.

    The marker is written FIRST and the legacy pointer is unlinked SECOND.
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
            f"original files are in {ARCHIVE_DIR_NAME}/ — KEEP them unless you have\n"
            f"checked the migrated copies contain everything you need.\n"
            f"Migration rewrites frontmatter: recognised fields are regenerated and\n"
            f"any UNRECOGNISED key is dropped, so such values survive ONLY here.\n"
        )

    legacy_pointer = memory_dir / "MEMORY.md"
    if legacy_pointer.exists():
        legacy_pointer.unlink()
