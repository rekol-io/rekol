"""Onboarding helpers for `rekol init`.

Pure detection + seeding logic (no prompts) lives here so it is unit-testable;
the interactive shell lives in ``rekol.cli_init``.
"""

from .detect import CloudSyncDir, count_claude_transcripts, detect_cloud_sync_dirs
from .scope import bootstrap_argv
from .starter_pack import find_template_dir, seed_starter_pack

__all__ = [
    "CloudSyncDir",
    "bootstrap_argv",
    "count_claude_transcripts",
    "detect_cloud_sync_dirs",
    "find_template_dir",
    "seed_starter_pack",
]
