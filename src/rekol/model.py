"""Memory file model: parsing and validation of YAML frontmatter.

Each memory file is a markdown document with a YAML frontmatter block.  This
module owns the canonical definition of what makes a memory file valid and
provides the single entry-point ``parse_file`` used by every other module.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_TYPES = frozenset({"always", "when", "topic", "knowledge"})
REQUIRED_FIELDS = ("name", "description", "type")

# scope is reserved for a future shared-team store but is NOT read or validated
# in v0.1. We accept any value (default "private") so that a memory file which
# already uses scope: informally still parses and indexes instead of being
# silently dropped. Validation + migration land when the shared store does.
DEFAULT_SCOPE = "private"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ValidationError(ValueError):
    """Raised when a memory file has missing or invalid frontmatter."""


@dataclass
class MemoryFile:
    """Parsed representation of a single memory markdown file.

    Args:
        path: Absolute path to the source file on disk.
        name: Human-readable name (``name`` frontmatter field).
        description: One-line description (``description`` frontmatter field).
        type: Memory layer — one of ``always``, ``when``, ``topic``, ``knowledge``.
        scope: Visibility scope — reserved for a future shared-team store.
            Defaults to ``"private"``.  Any value is accepted in v0.1
            (no validation) so that files already using ``scope:`` informally
            still parse and index.
        body: Markdown body text (everything after the frontmatter block).
        tags: Zero or more topic tags used for keyword search.
        aliases: Alternative names / search triggers for this memory.
        see_also: Relative paths to related memory files.
        created: ISO-8601 creation date string, if present.
        updated: ISO-8601 last-updated date string, if present.
        valid_from: ISO-8601 date this memory's facts started being true.
            Defaults to ``created`` when absent.  Bi-temporal model: lets you
            represent "what did I believe in March?" instead of overwriting.
        invalidated_at: ISO-8601 date this memory's facts stopped being true.
            ``None`` means the memory is still valid.
            ``ranking.apply_temporal_ranking`` excludes invalidated memories
            from default recall (opt-in via ``--include-invalidated``).
    """

    path: Path
    name: str
    description: str
    type: str
    body: str
    scope: str = DEFAULT_SCOPE
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    see_also: list[str] = field(default_factory=list)
    created: str | None = None
    updated: str | None = None
    valid_from: str | None = None
    invalidated_at: str | None = None

    @property
    def is_invalidated(self) -> bool:
        """True if this memory has been explicitly invalidated."""
        return self.invalidated_at is not None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _normalize_ts(value: object, *, date_only: bool) -> str | None:
    """Canonicalize a frontmatter timestamp to a single ISO format.

    PyYAML parses bare dates to ``date``/``datetime`` objects whose ``str()`` is
    space-separated, while the capture/invalidate CLIs write ``T``-separated ISO
    strings. Left unnormalized the index column would hold incompatible formats.
    ``date_only=True`` stores ``YYYY-MM-DD`` (created/updated/valid_from);
    otherwise full ISO with a ``T`` separator (invalidated_at).
    """
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):  # before date: datetime subclasses date
        return value.date().isoformat() if date_only else value.isoformat(timespec="seconds")
    if isinstance(value, _dt.date):
        return value.isoformat()
    s = str(value).strip()
    if date_only:
        return s[:10]
    return s.replace(" ", "T")


def parse_file(path: Path) -> MemoryFile:
    """Read *path*, parse its YAML frontmatter, validate, and return a :class:`MemoryFile`.

    Args:
        path: Path to a ``.md`` file with YAML frontmatter.

    Returns:
        A fully-populated :class:`MemoryFile` instance.

    Raises:
        ValidationError: If the file has no frontmatter block, is missing a
            required field, or uses an unsupported ``type`` value.
    """
    raw = path.read_text(encoding="utf-8")

    try:
        post = frontmatter.loads(raw)
    except Exception as exc:
        raise ValidationError(f"{path}: could not parse frontmatter: {exc}") from exc

    meta = post.metadata
    if not meta:
        raise ValidationError(f"{path}: missing frontmatter block")

    # Validate required fields are present and non-empty.
    for field_name in REQUIRED_FIELDS:
        if field_name not in meta or meta[field_name] in (None, ""):
            raise ValidationError(f"{path}: missing required field '{field_name}'")

    # Validate the type value against the allowed set.
    if meta["type"] not in ALLOWED_TYPES:
        raise ValidationError(
            f"{path}: invalid 'type' {meta['type']!r} (allowed: {sorted(ALLOWED_TYPES)})"
        )

    return MemoryFile(
        path=path,
        name=str(meta["name"]),
        description=str(meta["description"]),
        type=str(meta["type"]),
        scope=str(meta.get("scope", DEFAULT_SCOPE)),
        body=post.content,
        tags=_coerce_list(meta, "tags", path),
        aliases=_coerce_list(meta, "aliases", path),
        see_also=_coerce_list(meta, "see_also", path),
        created=_normalize_ts(meta.get("created"), date_only=True),
        updated=_normalize_ts(meta.get("updated"), date_only=True),
        valid_from=_normalize_ts(meta.get("valid_from"), date_only=True),
        invalidated_at=_normalize_ts(meta.get("invalidated_at"), date_only=False),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _coerce_list(meta: dict, key: str, path: Path) -> list[str]:
    """Return the frontmatter value for *key* as a list of strings.

    Treats a missing key or ``None`` as an empty list.  Raises
    :class:`ValidationError` if the value is present but not a list.

    Args:
        meta: Parsed frontmatter metadata dictionary.
        key: The field name to extract.
        path: Source file path, used only for error messages.

    Returns:
        A list of strings (may be empty).

    Raises:
        ValidationError: If the value exists but is not a list (including
            non-list scalars such as strings, integers, or booleans).
    """
    value = meta.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError(f"{path}: field '{key}' must be a list")
    return [str(item) for item in value]
