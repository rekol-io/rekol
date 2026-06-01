"""Onboarding helpers for `rekol init`.

Pure detection logic (no prompts) lives here so it is unit-testable; the
interactive shell lives in ``rekol.cli_init``.
"""

from .detect import (
    CloudSyncDir,
    count_claude_transcripts,
    count_curated_memory_files,
    detect_cloud_sync_dirs,
)

__all__ = [
    "CloudSyncDir",
    "count_claude_transcripts",
    "count_curated_memory_files",
    "detect_cloud_sync_dirs",
]
