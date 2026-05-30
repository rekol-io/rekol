"""Build synthetic Claude Code JSONL rows from a SessionGroup.

This is the only module that knows the ingester's row schema. A row must carry
``type`` in {user, assistant}, a ``uuid``, a ``sessionId``, a ``timestamp``, and
``message.content`` with extractable text (see sessions/ingest.py).

uuids and sessionIds are sha1 hashes salted with the archive ``prefix`` so two
different source trees converted under different prefixes can never collide on
the ingester's UNIQUE(session_id, message_uuid) key (which would otherwise drop
the second tree's files as dupes).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Dict, List

from .walk import SessionGroup

_SALT_SEP = "\0"


def _row_uuid(prefix: str, rel_to_source: str) -> str:
    return hashlib.sha1(f"{prefix}{_SALT_SEP}{rel_to_source}".encode("utf-8")).hexdigest()


def _session_id(prefix: str, session_rel_to_source: str) -> str:
    return hashlib.sha1(f"{prefix}{_SALT_SEP}{session_rel_to_source}".encode("utf-8")).hexdigest()


def _iso_z(mtime_unix: int) -> str:
    """Format a unix mtime as ISO-8601 UTC with a trailing Z (ingester-parseable)."""
    dt = datetime.fromtimestamp(mtime_unix, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_rows(group: SessionGroup, prefix: str, texts: Dict[str, str]) -> List[dict]:
    """Build one row per file whose extracted text is present in ``texts``.

    ``texts`` maps ``FileEntry.rel_to_session`` → extracted text. Files absent
    from the map (extraction returned None) produce no row. The path prefix is
    prepended to the content here, AFTER extraction's empty-check, so a
    path-only ghost message is never emitted.
    """
    session_id = _session_id(prefix, group.rel_to_source)
    rows: List[dict] = []
    for entry in group.files:
        text = texts.get(entry.rel_to_session)
        if text is None:
            continue
        content = f"{entry.rel_to_session}\n\n{text}"
        rows.append({
            "type": "user",
            "uuid": _row_uuid(prefix, entry.rel_to_source),
            "parentUuid": None,
            "sessionId": session_id,
            "timestamp": _iso_z(entry.mtime_unix),
            "cwd": group.folder_abs,
            "message": {"role": "document", "content": content},
        })
    return rows
