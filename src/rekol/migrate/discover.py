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

from dataclasses import dataclass
from pathlib import Path

from rekol.config import resolve_claude_config_dir

# Hidden marker file the migrator writes after successfully migrating a dir.
# Hidden so Claude Code's autoMemory does not inject it as context.
MIGRATION_MARKER_NAME = ".MIGRATED"

# Legacy marker — older installs wrote this string at the top of MEMORY.md.
# Kept here only so re-runs against an old-style retired dir still skip cleanly.
LEGACY_RETIREMENT_MARKER = "RETIRED — migrated to memory-tools"


@dataclass
class LegacyFile:
    """A discovered legacy memory file."""

    source_path: Path  # absolute path to the .md file
    source_root: Path  # the memory/ dir it belongs to
    project_slug: str  # parent dir name of source_root (for archive/rename)


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


def discover_files_in_dir(source_root: Path) -> list[LegacyFile]:
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
    files: list[LegacyFile] = []
    for entry in sorted(source_root.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix != ".md":
            continue
        if entry.name == "MEMORY.md":
            continue
        files.append(
            LegacyFile(
                source_path=entry,
                source_root=source_root,
                project_slug=project_slug,
            )
        )
    return files


def is_git_work_tree_root(directory: Path) -> bool:
    """Does ``directory`` hold a ``.git`` entry — i.e. is it a repo's root?

    Checked without shelling out to git: a work-tree root is exactly the
    directory containing ``.git``, which is a directory normally and a file in a
    worktree or submodule, so both are accepted.

    This exists because ``migrate repo`` treats any directory that directly
    contains ``.md`` files as a memory dir, and a repository root usually does —
    ``README.md``, ``CLAUDE.md``, ``CHANGELOG.md``. QA pointed it at a real repo
    and ``--commit`` moved the tracked ``README.md`` and ``CLAUDE.md`` into the
    archive, leaving them deleted in the work tree. Losing ``CLAUDE.md`` also
    takes a project's instructions out of Claude Code's view.

    Note this does NOT mean a git root can never be a memory dir: a notes repo
    is a perfectly reasonable migration source, and rekol's own ``git_track``
    makes ``REKOL_HOME`` a work-tree root. So the caller refuses by default and
    offers an explicit opt-in, rather than treating the shape as illegitimate.
    """
    return (directory / ".git").exists()


def is_auto_memory_dir(memory_dir: Path) -> bool:
    """Is ``memory_dir`` a Claude Code auto-memory dir — ``<projects>/<slug>/memory``?

    This decides whether a ``MEMORY.md`` there may be archived. Only autoMemory
    injection justifies moving that file: Claude Code injects
    ``~/.claude/projects/<slug>/memory/MEMORY.md`` into every session, so a
    migrated directory must not leave one behind. Anywhere else the file is
    quite possibly tracked by the user's own repository — rekol ships one in its
    template — and moving it would relocate tracked content and leave an
    untracked ``old-memory-archive/`` in their working tree.

    Resolved on both sides so a symlinked or relative path cannot slip past, and
    compared against the same ``resolve_claude_config_dir()`` the discovery
    above uses, so a relocated ``CLAUDE_CONFIG_DIR`` is honoured here too (#165).
    """
    projects_root = resolve_claude_config_dir() / "projects"
    try:
        resolved = memory_dir.resolve()
        root = projects_root.resolve()
    except OSError:
        return False
    if resolved.name != "memory":
        return False
    # Exactly <root>/<slug>/memory — not something buried deeper, whose MEMORY.md
    # autoMemory does not inject either.
    return resolved.parent.parent == root


def discover_auto_memory_sources() -> list[Path]:
    """Find all ~/.claude/projects/*/memory/ dirs that have not been migrated.

    Returns the list of <slug> dirs (parent of the memory/ subdir), since the
    migration marker and archive live in the same memory/ subdir.  Callers
    then pass ``<slug>/memory`` to ``discover_files_in_dir``.
    """
    # Honour CLAUDE_CONFIG_DIR like every other consumer (#165). This used a bare
    # `os.environ["HOME"]` subscript — a KeyError where the rest of the codebase
    # uses Path.home() — and hardcoded the tree, so a relocated Claude Code (or a
    # configured claude_projects_dir) made `rekol migrate auto` report "nothing to
    # migrate" rather than finding the user's legacy memory.
    projects_root = resolve_claude_config_dir() / "projects"
    if not projects_root.is_dir():
        return []
    out: list[Path] = []
    for slug_dir in sorted(projects_root.iterdir()):
        memory_dir = slug_dir / "memory"
        if not memory_dir.is_dir():
            continue
        if has_migration_marker(memory_dir):
            continue
        out.append(slug_dir)
    return out
