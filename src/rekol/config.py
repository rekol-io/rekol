"""Config loading from $REKOL_HOME/rekol.config.yaml (memory.config.yaml as fallback).

The data-directory location is read from ``$REKOL_HOME`` (primary), falling
back to ``$MEMORY_HOME`` so existing installs that only export ``MEMORY_HOME``
keep working.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

# Name of the skip manifest the indexer writes into the local cache (index_dir)
# after each run: the count + paths of on-disk files invisible to search because
# they were rejected at index time. Read by the SessionStart coverage hook (#123).
# Lives here (not in indexer.py) so the hook can read it without importing the
# indexer's heavy embedding stack just to resolve a filename.
SKIP_MANIFEST_NAME = "skipped.json"

DEFAULTS: dict = dict(
    embedding_model="BAAI/bge-small-en-v1.5",
    always_on_budget_bytes=8192,
    secret_check_on_capture=True,
    git_track=False,
    chunk_max_bytes=1500,
    claude_projects_dir="~/.claude/projects",
    session_search_enabled=True,
    temporal_exclude_invalidated=True,
    temporal_respect_valid_from=True,
    temporal_recency_weight=0.03,
    temporal_recency_halflife_days=180,
    temporal_recency_exempt_layers=["always", "knowledge"],
    temporal_confirm_interval_days=180,
    # --- Durable transcript archive (#8) ---
    archive_enabled=True,  # default-ON: we disclose at install, not opt-in
    archive_dir=None,  # None → resolve_archive_dir() picks the XDG default
    exclude_paths=[],  # glob patterns for project/cwd paths never archived/indexed
    # --- Include = scope (T8 #63) ---
    # Directories whose text files are kept in ongoing-indexing scope. A dir
    # included once auto-indexes new files on every session-index run; the
    # deny-list governs which files. BOTH default empty so an existing user's
    # behaviour is unchanged until they opt in (purely additive).
    include_dirs=[],  # list[str]: dir paths (~ and relative allowed; resolved on read)
    include_deny=[],  # list[str]: deny globs applied to files under include_dirs
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


def resolve_claude_config_dir() -> Path:
    """Resolve Claude Code's config tree, honouring ``CLAUDE_CONFIG_DIR``.

    Claude Code lets users relocate ``~/.claude`` via this variable. rekol
    hardcoded the default in several places, which meant a relocated tree got
    hooks written into a ``settings.json`` Claude Code never reads — while
    ``status`` reported them registered. Resolve it in exactly one place so the
    next consumer cannot reintroduce the hardcode.

    Branches on the RAW STRING: ``Path("")`` is ``PosixPath(".")`` and is
    truthy, so a ``Path(x) or fallback`` would silently never fall back.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "")
    return Path(os.path.expanduser(raw)) if raw.strip() else Path.home() / ".claude"


def resolve_index_dir(memory_home: Path) -> Path:
    """Resolve the local-only cache dir that holds all derived index state.

    SECURITY: the index — including ``sessions.db``, which records every prompt
    and assistant turn verbatim (and thus any pasted secrets) — must never live
    inside ``$REKOL_HOME``. ``$REKOL_HOME`` is a user-chosen folder that may be
    synced via Dropbox / iCloud / Google Drive / OneDrive / Syncthing / git; a
    per-tool ignore file (the old ``.dropboxignore``) only protects Dropbox.
    Relocating the index OUT of the synced tree into a machine-local cache kills
    that whole class of exposure regardless of sync tool.

    Resolution order:
        1. ``$REKOL_INDEX_DIR`` — explicit override, used verbatim.
        2. ``${XDG_CACHE_HOME:-~/.cache}/rekol/<hash>`` where ``<hash>`` is a
           stable 16-hex-char SHA-256 of the resolved ``memory_home`` path, so
           multiple ``REKOL_HOME`` roots get distinct, non-colliding caches.

    Args:
        memory_home: The resolved (expanded, absolute) memory-home path.

    Returns:
        The absolute index/cache directory path (not created here; the SQLite
        stores ``mkdir(parents=True)`` their own parent on open).
    """
    override = os.environ.get("REKOL_INDEX_DIR")
    if override:
        return Path(os.path.expanduser(override))
    cache_home = os.environ.get("XDG_CACHE_HOME")
    cache_root = Path(os.path.expanduser(cache_home)) if cache_home else Path.home() / ".cache"
    digest = hashlib.sha256(str(memory_home).encode()).hexdigest()[:16]
    return cache_root / "rekol" / digest


def resolve_archive_dir(config_archive_dir: str | None) -> Path:
    """Resolve the durable, rekol-owned transcript archive directory.

    Unlike :func:`resolve_index_dir`, the archive is **machine-level** — NOT
    hashed per ``$REKOL_HOME``. Transcripts come from ``~/.claude/projects``
    regardless of which memory home is active, so one archive per machine is
    correct; splitting per-home would only duplicate the same transcripts.

    SECURITY: the archive holds verbatim prompts (and any pasted secrets), so it
    defaults to a LOCAL, non-synced, non-cache location — the same posture that
    moved ``sessions.db`` out of ``$REKOL_HOME`` (#10/#13). It lives under
    ``XDG_DATA_HOME`` (durable) rather than ``XDG_CACHE_HOME`` (disposable),
    because it is the source of truth a rebuild reads from, not a rebuildable
    cache.

    Resolution order:
        1. ``$REKOL_ARCHIVE_DIR`` — explicit override, used verbatim (expanded).
        2. ``config_archive_dir`` — the ``archive_dir`` config key, if set.
        3. ``${XDG_DATA_HOME:-~/.local/share}/rekol/archive``.

    Args:
        config_archive_dir: The raw ``archive_dir`` config value, or ``None``.

    Returns:
        The absolute archive directory path (not created here; the archive
        module ``mkdir(parents=True)``s it on first write).
    """
    override = os.environ.get("REKOL_ARCHIVE_DIR")
    if override:
        return Path(os.path.expanduser(override))
    if config_archive_dir:
        return Path(os.path.expanduser(config_archive_dir))
    data_home = os.environ.get("XDG_DATA_HOME")
    data_root = (
        Path(os.path.expanduser(data_home)) if data_home else Path.home() / ".local" / "share"
    )
    return data_root / "rekol" / "archive"


def path_is_excluded(path: str, patterns: list[str]) -> bool:
    """True when ``path`` matches any exclude glob in ``patterns``.

    The shared exclude matcher — the reusable foundation for #5; the archive
    sink is its first consumer. Matching is over the path's STRING form with
    ``fnmatch`` (case-sensitive, ``*`` spans any characters incl. ``/`` since we
    match the whole path, not per-segment). A bare segment like
    ``secret-project`` is matched anywhere in the path by also testing
    ``*/<pattern>/*`` and ``*<pattern>*``, so users need not always write a full
    glob.

    Empty ``patterns`` excludes nothing (the default — nothing is excluded until
    the user opts in).
    """
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        # A bare segment (no glob chars) should still match anywhere in the path
        # so a user can write `secret-project` instead of `*/secret-project/*`.
        if "*" not in pattern and "?" not in pattern:
            if fnmatch.fnmatch(path, f"*/{pattern}/*") or fnmatch.fnmatch(path, f"*{pattern}*"):
                return True
    return False


def load_rekolignore_patterns(root: Path) -> list[str]:
    """Read ``<root>/.rekolignore`` (gitignore-style) into a pattern list.

    Honored IN ADDITION to ``exclude_paths`` from config. Blank lines and lines
    starting with ``#`` are skipped; each remaining line is stripped of
    surrounding whitespace and used as an ``fnmatch`` glob. A missing file
    yields an empty list (no error — absence means "ignore nothing here").
    """
    ignore_file = root / ".rekolignore"
    if not ignore_file.is_file():
        return []
    patterns: list[str] = []
    # OSError (permission, race) must not crash a sync — an unreadable ignore
    # file degrades to "no extra patterns", logged by the caller, never fatal.
    try:
        text = ignore_file.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


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
    temporal_exclude_invalidated: bool
    temporal_respect_valid_from: bool
    temporal_recency_weight: float
    temporal_recency_halflife_days: float
    temporal_recency_exempt_layers: list[str]
    temporal_confirm_interval_days: int
    archive_enabled: bool
    exclude_paths: list[str]
    # Include = scope (T8 #63): RAW string lists as written in the YAML. Paths are
    # resolved lazily via resolved_include_dirs() (mirrors archive_dir) so a ~ or a
    # relative path is expanded at call time, not frozen at load. Both default empty.
    include_dirs: list[str]
    include_deny: list[str]
    # NOTE: the RESOLVED archive_dir is intentionally NOT a stored field — it is
    # resolved lazily via the archive_dir property (mirrors index_dir), so an env
    # override is honored at call time rather than frozen at load time. We store
    # only the RAW config value here (no leading underscore — this is a public
    # dataclass field; the resolved value comes from the property).
    archive_dir_raw: str | None

    @property
    def index_dir(self) -> Path:
        """Absolute path to the local-only cache dir holding all derived state.

        Lives OUTSIDE ``$REKOL_HOME`` (in ``$XDG_CACHE_HOME``/``~/.cache``) so
        the secrets-bearing ``sessions.db`` and the rest of the index never sit
        in a sync-able folder. See :func:`resolve_index_dir`.
        """
        return resolve_index_dir(self.memory_home)

    @property
    def index_db_path(self) -> Path:
        """Absolute path to the SQLite curated-memory index database.

        Returns:
            Path at ``<cache>/index.db`` (outside ``$REKOL_HOME``).
        """
        return self.index_dir / "index.db"

    @property
    def sessions_db_path(self) -> Path:
        """Absolute path to the SQLite sessions database (transcripts index).

        ``sessions.db`` captures every prompt/assistant turn verbatim, so it is
        kept in the local-only cache (outside ``$REKOL_HOME``) and never synced.

        Returns:
            Path at ``<cache>/sessions.db`` (outside ``$REKOL_HOME``).
        """
        return self.index_dir / "sessions.db"

    @property
    def pending_review_dir(self) -> Path:
        """Absolute path to the bootstrap/propose human-review queue.

        Lives in the local-only cache (under :attr:`index_dir`), NOT in
        ``$REKOL_HOME`` (#57). The queue holds recalled *raw transcript*
        candidates the user hasn't curated yet, so keeping it out of the synced
        tree means raw transcripts never reach a sync provider — the curated
        memory the user approves is what lands in ``$REKOL_HOME`` and syncs.
        Colocating it with the index also means a cache wipe / uninstall clears
        the queue for free.

        Returns:
            Path at ``<cache>/pending-review`` (outside ``$REKOL_HOME``).
        """
        return self.index_dir / "pending-review"

    @property
    def archive_dir(self) -> Path:
        """Absolute path to the durable transcript archive (see resolve_archive_dir).

        Machine-level and NOT synced by default; holds verbatim transcripts.
        Resolved lazily so an env override is honored at call time.
        """
        return resolve_archive_dir(self.archive_dir_raw)

    def resolved_include_dirs(self) -> list[Path]:
        """Absolute, ~-expanded include-scope dirs (T8 #63).

        Resolved lazily (not a frozen field) so a ``~`` or a relative path in the
        YAML is expanded at call time — the same posture as ``archive_dir``. Order
        follows the config list (deterministic). De-dupes exact repeats while
        preserving first-seen order, so an accidental duplicate dir is not
        indexed twice. A method (not a property) because it does filesystem-
        agnostic path work that reads as a small computation, not an attribute.
        """
        seen: set[Path] = set()
        resolved: list[Path] = []
        for raw in self.include_dirs:
            path = Path(os.path.expanduser(str(raw)))
            if not path.is_absolute():
                # Relative include dirs are anchored at the memory home so they
                # mean the same thing regardless of the process's cwd.
                path = (self.memory_home / path).resolve()
            if path not in seen:
                seen.add(path)
                resolved.append(path)
        return resolved


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
        temporal_exclude_invalidated=bool(data["temporal_exclude_invalidated"]),
        temporal_respect_valid_from=bool(data["temporal_respect_valid_from"]),
        temporal_recency_weight=float(data["temporal_recency_weight"]),
        temporal_recency_halflife_days=float(data["temporal_recency_halflife_days"]),
        temporal_recency_exempt_layers=list(data["temporal_recency_exempt_layers"]),
        temporal_confirm_interval_days=int(data["temporal_confirm_interval_days"]),
        archive_enabled=bool(data["archive_enabled"]),
        exclude_paths=list(data["exclude_paths"]),
        include_dirs=[str(d) for d in data["include_dirs"]],
        include_deny=[str(d) for d in data["include_deny"]],
        archive_dir_raw=(str(data["archive_dir"]) if data["archive_dir"] is not None else None),
    )


def persist_include_scope(
    memory_home: Path, include_dirs: list[str], include_deny: list[str]
) -> None:
    """Write the include-scope keys back into ``<memory_home>/rekol.config.yaml``.

    This is what makes the scope "editable on rerun" (the spec): the onboarding
    flow calls this with the user's current picks, overwriting any prior values
    for these two keys while PRESERVING every other key in the file. We round-trip
    through ``yaml.safe_load`` so we never clobber unrelated config; a missing or
    empty file starts from ``{}``.

    Reads the existing ``rekol.config.yaml`` (NOT the ``memory.config.yaml``
    fallback — new writes always go to the current filename). On a malformed
    existing file we raise rather than silently dropping the user's other keys.
    """
    config_file = memory_home / "rekol.config.yaml"
    existing: dict = {}
    if config_file.exists():
        # A malformed config must not be silently overwritten — that would lose
        # the user's other keys. Surface it so they can fix it.
        try:
            loaded = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(
                f"Cannot persist include-scope: {config_file} is not valid YAML: {exc}"
            ) from exc
        if loaded is not None:
            if not isinstance(loaded, dict):
                raise ValueError(
                    f"Cannot persist include-scope: {config_file} top level is not a mapping"
                )
            existing = loaded
    existing["include_dirs"] = list(include_dirs)
    existing["include_deny"] = list(include_deny)
    memory_home.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        yaml.safe_dump(existing, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
