"""Archive legacy memory files and leave a retirement pointer.

The archive lives under ``<memory_dir>/old-memory-archive/`` so the user can
recover originals for one week (policy: delete after 1 week from migration
date; the pointer records the date).
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from memory_tools.migrate.discover import LegacyFile, is_retirement_pointer


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
    """Replace ``memory_dir/MEMORY.md`` with a retirement pointer.

    Idempotent: if the file is already a retirement pointer, leaves it alone.
    """
    memory_md = memory_dir / "MEMORY.md"
    if memory_md.exists() and is_retirement_pointer(memory_md):
        return
    today = dt.date.today().isoformat()
    body = (
        f"# RETIRED — migrated to memory-tools $MEMORY_HOME ({today})\n\n"
        f"All cross-session context for this project has been migrated to:\n\n"
        f"- **Location:** `{memory_home}`\n"
        f"- **Always-loaded index:** `{memory_home}/MEMORY.md`\n"
        f"- **Semantic search:** `memory-search \"query\" --top N`\n"
        f"- **Write new memories:** `memory-capture --layer {{always|when|topic|knowledge}} "
        f"--file <name>.md ...` (see `memory` skill)\n\n"
        f"## For Claude\n\n"
        f"Do NOT write new memories to this file or to this directory. "
        f"Write to `$MEMORY_HOME` via the `memory` skill instead.\n\n"
        f"Archived copies of the pre-migration files live in `{ARCHIVE_DIR_NAME}/` "
        f"alongside this file — safe to delete after 1 week.\n"
    )
    memory_md.write_text(body)
