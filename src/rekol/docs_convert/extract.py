"""Read a source file to plain text, or return None when it is not indexable.

This is the only module aware of file types. Best-effort: never raises on a
single bad file — callers count the None/exception and continue.
"""

from __future__ import annotations

from pathlib import Path

from . import TEXT_EXTENSIONS


def is_text_native(path: Path, text_extensions: frozenset[str] | None = None) -> bool:
    """True when the file's extension is one we read as plain text.

    ``text_extensions`` overrides the default :data:`TEXT_EXTENSIONS` allowlist
    (the --include/--exclude basis). Extensions are lowercase, no leading dot.
    """
    exts = TEXT_EXTENSIONS if text_extensions is None else text_extensions
    return path.suffix.lstrip(".").lower() in exts


def extract_text(path: Path, max_bytes: int) -> str | None:
    """Return the file's text, or None if it should be skipped.

    Returns None when:
      - the file is larger than ``max_bytes`` (backstop against one pathological
        file becoming one giant FTS row),
      - the content is empty or whitespace-only after reading.

    Decoding uses ``errors="replace"`` so a few bad bytes degrade gracefully
    rather than aborting the whole conversion. The empty-check runs here, BEFORE
    any path-prefixing in transcript.py, so an empty file never becomes a
    path-only "ghost" message.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > max_bytes:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text if text.strip() else None
