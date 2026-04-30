"""Legacy memory discovery: find source dirs and files to migrate.

A "source dir" is a directory that contains legacy markdown memory files — either
the auto-memory directory (~/.claude/projects/<slug>/memory/) or a user-supplied
repo subdir.  A source dir is eligible for migration when its MEMORY.md (if
present) is not already a retirement pointer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List


RETIREMENT_MARKER = "RETIRED — migrated to memory-tools"


@dataclass
class LegacyFile:
    """A discovered legacy memory file."""
    source_path: Path       # absolute path to the .md file
    source_root: Path       # the memory/ dir it belongs to
    project_slug: str       # parent dir name of source_root (for archive/rename)


def is_retirement_pointer(memory_md: Path) -> bool:
    """Return True if ``memory_md`` exists and starts with the retirement marker."""
    try:
        head = memory_md.read_text(encoding="utf-8", errors="replace").lstrip()
    except FileNotFoundError:
        return False
    return RETIREMENT_MARKER in head.splitlines()[0] if head else False


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
    """Find all ~/.claude/projects/*/memory/ dirs whose MEMORY.md is not retired.

    Returns the list of <slug> dirs (parent of the memory/ subdir), since the
    retirement pointer and archive live in the same memory/ subdir.  Callers
    then pass ``<slug>/memory`` to ``discover_files_in_dir``.
    """
    home = Path(os.environ["HOME"])
    projects_root = home / ".claude" / "projects"
    if not projects_root.is_dir():
        return []
    out: List[Path] = []
    for slug_dir in sorted(projects_root.iterdir()):
        memory_dir = slug_dir / "memory"
        memory_md = memory_dir / "MEMORY.md"
        if not memory_dir.is_dir():
            continue
        if is_retirement_pointer(memory_md):
            continue
        out.append(slug_dir)
    return out
