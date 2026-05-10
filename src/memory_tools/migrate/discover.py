"""Legacy memory discovery: find source dirs and files to migrate.

A "source dir" is a directory that contains legacy markdown memory files — either
the auto-memory directory (~/.claude/projects/<slug>/memory/) or a user-supplied
repo subdir.  A source dir is eligible for migration when its hidden ``.MIGRATED``
marker file is absent.

Historical note: previous versions of this tool wrote a ``MEMORY.md`` retirement
pointer instead of ``.MIGRATED``.  Claude Code's built-in autoMemory feature
auto-injects any ``MEMORY.md`` it finds under ``~/.claude/projects/<slug>/memory/``,
so a tombstone there ended up as injected context (with literal ``$MEMORY_HOME``
that Claude could not resolve).  We now use a hidden marker instead, and treat
the legacy ``MEMORY.md`` retirement string as "also already migrated" for
backward compatibility on machines installed under the old version.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List


# Hidden marker file the migrator writes after successfully migrating a dir.
# Hidden so Claude Code's autoMemory does not inject it as context.
MIGRATION_MARKER_NAME = ".MIGRATED"

# Legacy marker — older installs wrote this string at the top of MEMORY.md.
# Kept here only so re-runs against an old-style retired dir still skip cleanly.
LEGACY_RETIREMENT_MARKER = "RETIRED — migrated to memory-tools"


@dataclass
class LegacyFile:
    """A discovered legacy memory file."""
    source_path: Path       # absolute path to the .md file
    source_root: Path       # the memory/ dir it belongs to
    project_slug: str       # parent dir name of source_root (for archive/rename)


def is_retirement_pointer(memory_md: Path) -> bool:
    """Return True if ``memory_md`` is a legacy MEMORY.md retirement pointer.

    Kept for backward compatibility with installs that ran older versions of
    this tool.  New installs use ``has_migration_marker`` instead.
    """
    try:
        head = memory_md.read_text(encoding="utf-8", errors="replace").lstrip()
    except FileNotFoundError:
        return False
    return LEGACY_RETIREMENT_MARKER in head.splitlines()[0] if head else False


def has_migration_marker(memory_dir: Path) -> bool:
    """Return True if ``memory_dir`` has been previously migrated.

    Checks for the hidden ``.MIGRATED`` marker first (current convention), then
    falls back to the legacy ``MEMORY.md`` retirement string for old installs.
    """
    if (memory_dir / MIGRATION_MARKER_NAME).is_file():
        return True
    return is_retirement_pointer(memory_dir / "MEMORY.md")


def discover_files_in_dir(source_root: Path) -> List[LegacyFile]:
    """Return every .md file directly under ``source_root`` except MEMORY.md.

    Does NOT recurse into subdirs — the ``old-memory-archive/`` dir (and any
    other subdir) is ignored by design.  ``source_root`` itself must exist.
    """
    if not source_root.is_dir():
        return []
    # Derive project slug from the PARENT of source_root (e.g. memory/ -> its parent).
    # For auto-memory: ~/.claude/projects/<slug>/memory/ → slug is parent.name.
    # For a repo subdir: /repo/docs/memory/ → slug is parent.name ("docs").
    project_slug = source_root.parent.name
    files: List[LegacyFile] = []
    for entry in sorted(source_root.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix != ".md":
            continue
        if entry.name == "MEMORY.md":
            continue
        files.append(LegacyFile(
            source_path=entry,
            source_root=source_root,
            project_slug=project_slug,
        ))
    return files


def discover_auto_memory_sources() -> List[Path]:
    """Find all ~/.claude/projects/*/memory/ dirs that have not been migrated.

    Returns the list of <slug> dirs (parent of the memory/ subdir), since the
    migration marker and archive live in the same memory/ subdir.  Callers
    then pass ``<slug>/memory`` to ``discover_files_in_dir``.
    """
    home = Path(os.environ["HOME"])
    projects_root = home / ".claude" / "projects"
    if not projects_root.is_dir():
        return []
    out: List[Path] = []
    for slug_dir in sorted(projects_root.iterdir()):
        memory_dir = slug_dir / "memory"
        if not memory_dir.is_dir():
            continue
        if has_migration_marker(memory_dir):
            continue
        out.append(slug_dir)
    return out
