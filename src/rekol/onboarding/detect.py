"""Pure detection helpers used by `rekol init` — no prompts, no side effects."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CloudSyncDir:
    """A detected cloud-sync folder offered as a REKOL_HOME location."""

    label: str
    path: Path


def count_claude_transcripts(projects_dir: Path) -> int:
    """Count Claude Code transcript files (``*.jsonl``) under ``projects_dir``.

    Returns 0 when the directory does not exist. This is the headline first-run
    value signal: a large count means install can turn an empty store into a
    searchable history of past work.
    """
    if not projects_dir.is_dir():
        return 0
    # rglob defaults to recurse_symlinks=False (Python 3.13+), so no loop risk.
    # On a large ~/.claude/projects this scan runs before the first prompt; if
    # latency on network-synced filesystems becomes noticeable, add an early-exit cap.
    return sum(1 for _ in projects_dir.rglob("*.jsonl"))


# Top-level files/dirs that are infrastructure, not user-authored memory. The
# index files (REKOL.md / MEMORY.md) ship with a freshly-seeded store, and the
# .index/ + .install-logs/ dirs are machine-only — none of them indicate that
# the user has accumulated real memories yet.
_NON_CONTENT_TOP_LEVEL = frozenset({"REKOL.md", "MEMORY.md"})
_NON_CONTENT_DIRS = frozenset({".index", ".install-logs", ".git"})


def count_curated_memory_files(memory_home: Path) -> int:
    """Count user-authored curated-memory markdown files under ``memory_home``.

    Mirrors install.sh's ``is_empty_memory_home`` intent but at markdown
    granularity: a store seeded only from ``template/`` (which ships the
    always-on index and a couple of starter layer files) is still effectively
    "near-empty", so the SessionStart nudge uses a small threshold against this
    count rather than a strict zero.

    Excludes the always-on index files (``REKOL.md``/``MEMORY.md``) and the
    machine-only ``.index/``/``.install-logs/``/``.git/`` dirs. Counts only
    ``*.md`` files so config YAML and stray ``.txt`` notes do not inflate the
    signal. Returns 0 when ``memory_home`` does not exist.
    """
    if not memory_home.is_dir():
        return 0
    count = 0
    # rglob defaults to recurse_symlinks=False (Python 3.13+), so no loop risk.
    for md in memory_home.rglob("*.md"):
        rel = md.relative_to(memory_home)
        if rel.parts and rel.parts[0] in _NON_CONTENT_DIRS:
            continue
        if len(rel.parts) == 1 and rel.name in _NON_CONTENT_TOP_LEVEL:
            continue
        count += 1
    return count


def default_cloud_sync_candidates() -> dict[str, Path]:
    """Standard macOS cloud-sync folder candidates, keyed by display label.

    These paths are macOS-specific; Linux installs should pass their own candidates
    dict or extend this with platform branching.
    """
    home = Path(os.path.expanduser("~"))
    return {
        "Dropbox": home / "Dropbox",
        "iCloud Drive": home / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
        "Google Drive": home / "Google Drive",
        "OneDrive": home / "OneDrive",
    }


def detect_cloud_sync_dirs(
    candidates: dict[str, Path] | None = None,
) -> list[CloudSyncDir]:
    """Return the subset of ``candidates`` that actually exist on disk.

    Order follows ``candidates`` insertion order so the output is deterministic.
    """
    if candidates is None:
        candidates = default_cloud_sync_candidates()
    return [
        CloudSyncDir(label=label, path=path) for label, path in candidates.items() if path.is_dir()
    ]
