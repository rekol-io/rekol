"""Shared test helper: resolve the local index cache dir for a memory home.

The index now lives in a machine-local cache at ``${XDG_CACHE_HOME:-~/.cache}/
rekol/<hash>`` (outside ``$REKOL_HOME``). Tests use this to assert against the
same location the CLI/config write to, rather than hard-coding the hash.
"""

from __future__ import annotations

import os
from pathlib import Path

from rekol.config import resolve_index_dir


def cache_dir_for(memory_home: Path) -> Path:
    """Resolve the index cache dir for a given memory home (mirrors runtime)."""
    return resolve_index_dir(Path(os.path.expanduser(str(memory_home))))
