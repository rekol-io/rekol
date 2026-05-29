"""Config loading from $MEMORY_HOME/memory.config.yaml, with defaults."""
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


@dataclass
class Config:
    """Resolved configuration for memory-tools, derived from $MEMORY_HOME/memory.config.yaml.

    Unknown keys in the YAML file are silently ignored so that future
    config additions remain forward-compatible with older tool versions.
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
            Path at ``$MEMORY_HOME/.index/index.db``.
        """
        return self.memory_home / ".index" / "index.db"

    @property
    def sessions_db_path(self) -> Path:
        """Absolute path to the SQLite sessions database (transcripts index).

        Returns:
            Path at ``$MEMORY_HOME/.index/sessions.db``.
        """
        return self.memory_home / ".index" / "sessions.db"


def load_config() -> Config:
    """Load and validate configuration from $MEMORY_HOME.

    Reads ``$MEMORY_HOME/memory.config.yaml`` when it exists; falls back to
    :data:`DEFAULTS` for any key that is absent. Unknown keys in the YAML
    are silently ignored (forward-compatible).

    Returns:
        A fully populated :class:`Config` instance.

    Raises:
        RuntimeError: If the ``MEMORY_HOME`` environment variable is not set.
    """
    env = os.environ.get("MEMORY_HOME")
    if not env:
        raise RuntimeError(
            "MEMORY_HOME environment variable is not set. "
            "Export it to point at your memory home directory before running memory-tools."
        )
    root = Path(os.path.expanduser(env))
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
