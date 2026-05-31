"""Config loading from $REKOL_HOME/rekol.config.yaml (memory.config.yaml as fallback).

The data-directory location is read from ``$REKOL_HOME`` (primary), falling
back to ``$MEMORY_HOME`` so existing installs that only export ``MEMORY_HOME``
keep working.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULTS: dict = dict(
    embedding_model="BAAI/bge-small-en-v1.5",
    always_on_budget_bytes=8192,
    secret_check_on_capture=True,
    git_track=False,
    chunk_max_bytes=1500,
    claude_projects_dir="~/.claude/projects",
    session_search_enabled=True,
)


def resolve_memory_home() -> str | None:
    """Resolve the data-directory location from the environment.

    ``REKOL_HOME`` is the primary variable; ``MEMORY_HOME`` is kept as a
    fallback so existing installs (which export ``MEMORY_HOME``) keep working.

    Returns:
        The raw (un-expanded) directory string, or ``None`` if neither
        variable is set.
    """
    return os.environ.get("REKOL_HOME") or os.environ.get("MEMORY_HOME")


@dataclass
class Config:
    """Resolved configuration for rekol.

    Derived from $REKOL_HOME/rekol.config.yaml (memory.config.yaml as fallback).
    The data-directory location is read from ``$REKOL_HOME`` (primary), falling
    back to ``$MEMORY_HOME``. Unknown keys in the YAML file are silently ignored
    so that future config additions remain forward-compatible with older tool
    versions.
    """

    memory_home: Path
    embedding_model: str
    always_on_budget_bytes: int
    secret_check_on_capture: bool
    git_track: bool
    chunk_max_bytes: int
    claude_projects_dir: Path
    session_search_enabled: bool

    @property
    def index_db_path(self) -> Path:
        """Absolute path to the SQLite index database.

        Returns:
            Path at ``$REKOL_HOME/.index/index.db``.
        """
        return self.memory_home / ".index" / "index.db"

    @property
    def sessions_db_path(self) -> Path:
        """Absolute path to the SQLite sessions database (transcripts index).

        Returns:
            Path at ``$REKOL_HOME/.index/sessions.db``.
        """
        return self.memory_home / ".index" / "sessions.db"


def load_config() -> Config:
    """Load and validate configuration from $REKOL_HOME (or $MEMORY_HOME).

    Reads ``$REKOL_HOME/rekol.config.yaml`` (``memory.config.yaml`` as fallback)
    when it exists; falls back to :data:`DEFAULTS` for any key that is absent.
    Unknown keys in the YAML are silently ignored (forward-compatible).

    Returns:
        A fully populated :class:`Config` instance.

    Raises:
        RuntimeError: If neither ``REKOL_HOME`` nor ``MEMORY_HOME`` is set.
    """
    # REKOL_HOME is the primary data-directory variable; MEMORY_HOME is kept as
    # a fallback so existing installs (which export MEMORY_HOME) keep working.
    env = resolve_memory_home()
    if not env:
        raise RuntimeError(
            "Neither REKOL_HOME nor MEMORY_HOME is set. Export REKOL_HOME to point "
            "at your memory home directory before running rekol "
            "(MEMORY_HOME is accepted as a fallback)."
        )
    root = Path(os.path.expanduser(env))
    # rekol.config.yaml is the current name; memory.config.yaml is read as a
    # fallback so memory roots created by older versions keep working untouched.
    config_file = root / "rekol.config.yaml"
    if not config_file.exists():
        config_file = root / "memory.config.yaml"
    data: dict = dict(DEFAULTS)
    if config_file.exists():
        loaded = yaml.safe_load(config_file.read_text()) or {}
        # Only accept keys that exist in DEFAULTS; unknown keys are silently
        # dropped so that newer config files don't break older tool versions.
        data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
    return Config(
        memory_home=root,
        embedding_model=str(data["embedding_model"]),
        always_on_budget_bytes=int(data["always_on_budget_bytes"]),
        secret_check_on_capture=bool(data["secret_check_on_capture"]),
        git_track=bool(data["git_track"]),
        chunk_max_bytes=int(data["chunk_max_bytes"]),
        claude_projects_dir=Path(os.path.expanduser(str(data["claude_projects_dir"]))),
        session_search_enabled=bool(data["session_search_enabled"]),
    )
