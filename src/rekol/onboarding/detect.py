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
    # Early-exit cap: this scan runs before the first prompt, and on a large or
    # network-synced ~/.claude/projects an unbounded walk is slow. The count only
    # drives a >0 Path-A/B fork and a headline number, so stop once we've seen
    # enough to know there's "a lot" rather than walking every file.
    cap = 10_000
    count = 0
    for _ in projects_dir.rglob("*.jsonl"):
        count += 1
        if count >= cap:
            break
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
