"""Read Claude Code ~/.claude/projects/*/*.jsonl transcripts, normalise rows, write to SessionStore.

Filters non-message rows (queue-operation, etc.) and normalises content that
may be a string or a list of content blocks into a single text payload. Dedupe
relies on the UNIQUE(session_id, message_uuid) constraint in the store.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .store import SessionStore


@dataclass
class IngestStats:
    files_seen: int = 0
    files_ingested: int = 0
    files_skipped_unchanged: int = 0  # mtime+size match — already ingested
    messages_inserted: int = 0
    messages_skipped_dupe: int = 0
    messages_skipped_malformed: int = 0  # parse error or missing required field
    messages_skipped_no_text: int = 0    # row IS a message but has no indexable text
                                          # (assistant tool_use only, thinking only,
                                          #  user tool_result only). This is the dominant
                                          #  bucket on real transcripts; tracked separately
                                          #  so the user can see the real drop rate vs
                                          #  parse errors.


# Row types in the JSONL stream we treat as messages.
_MESSAGE_TYPES = ("user", "assistant")


def _flatten_content(content) -> Optional[str]:
    """Convert message.content to plain text. Returns None if there's nothing usable."""
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                # Common shapes: {"type": "text", "text": "..."} and tool blocks
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
                # Tool-use blocks have input dicts; skip them for text indexing.
                # Tool-result blocks have a content field we deliberately do
                # NOT index — that's command output, not user/assistant prose.
        joined = " ".join(p.strip() for p in parts if p and p.strip())
        return joined or None
    return None


def _parse_timestamp(ts: str) -> tuple[str, int]:
    """Return (iso_string, unix_seconds). Handles trailing Z suffix."""
    iso = ts
    parseable = ts.rstrip("Z") + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(parseable)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return iso, int(dt.timestamp())


@dataclass
class _RawIterResult:
    """Internal carrier so iter_messages_in_file can report both yielded messages
    and rows skipped for "no indexable text" without forcing the caller to
    consume two iterators. The stats are mutated by the iterator; the caller
    reads them after the iteration completes.
    """
    no_text_count: int = 0
    malformed_count: int = 0


def iter_messages_in_file(jsonl_path: Path, stats: Optional[_RawIterResult] = None) -> Iterator[dict]:
    """Yield normalised message dicts ready for SessionStore.insert_message.

    Skips non-message rows (queue-operation, attachment, etc.) entirely.
    For rows whose ``type`` is ``user``/``assistant`` but whose content has
    no indexable text (assistant tool_use only, thinking only, user
    tool_result only — the dominant case in real transcripts), the row is
    counted in ``stats.no_text_count`` rather than yielded. Malformed rows
    (JSON decode error, missing required field, bad timestamp) bump
    ``stats.malformed_count``.

    ``line_number`` is the 1-indexed line in the file at which the message
    was found.
    """
    path = Path(jsonl_path)
    with path.open("r", encoding="utf-8") as fh:
        for line_number, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                if stats is not None:
                    stats.malformed_count += 1
                continue
            row_type = row.get("type")
            if row_type not in _MESSAGE_TYPES:
                continue  # not a message row at all — silent, not counted
            uuid = row.get("uuid")
            session_id = row.get("sessionId")
            timestamp = row.get("timestamp")
            message = row.get("message") or {}
            if not uuid or not session_id or not timestamp:
                if stats is not None:
                    stats.malformed_count += 1
                continue
            content = _flatten_content(message.get("content"))
            if not content:
                # IS a message turn, but no text to index (tool_use, thinking,
                # tool_result without text). Track separately so users can see
                # the v1 framing: "human-typed + assistant-text turns only".
                if stats is not None:
                    stats.no_text_count += 1
                continue
            try:
                iso, unix = _parse_timestamp(timestamp)
            except ValueError:
                if stats is not None:
                    stats.malformed_count += 1
                continue
            yield dict(
                session_id=session_id,
                message_uuid=uuid,
                parent_uuid=row.get("parentUuid"),
                role=message.get("role") or row_type,
                content=content,
                cwd=row.get("cwd"),
                timestamp_iso=iso,
                timestamp_unix=unix,
                jsonl_path=str(path),
                line_number=line_number,
            )


def ingest_file(jsonl_path: Path, store: SessionStore, *, force: bool = False) -> IngestStats:
    """Ingest a single JSONL file.

    Honours mtime+size skip via ``files_seen``: if the file is unchanged
    since last ingest, returns a stats record with ``files_skipped_unchanged=1``
    and no other work. Set ``force=True`` to bypass the skip (used by
    ``--full`` mode).

    All inserts for the file happen in a single transaction (BEGIN ... COMMIT)
    to avoid the per-row fsync cost that would otherwise dominate backfill on
    machines with deep transcript history.
    """
    path = Path(jsonl_path)
    stat = path.stat()
    mtime_unix = int(stat.st_mtime)
    size_bytes = int(stat.st_size)

    stats = IngestStats(files_seen=1)
    if not force and store.should_skip_file(str(path), mtime_unix, size_bytes):
        stats.files_skipped_unchanged = 1
        return stats

    raw_stats = _RawIterResult()
    # Batched transaction — single commit per file rather than per row.
    # Avoids ~100k fsyncs during initial backfill on deep-history machines.
    store.conn.execute("BEGIN")
    try:
        for msg in iter_messages_in_file(path, raw_stats):
            rowid = store.insert_message_no_commit(msg)
            if rowid is None:
                stats.messages_skipped_dupe += 1
            else:
                stats.messages_inserted += 1
        store.conn.commit()
    except Exception:
        store.conn.rollback()
        raise

    # files_seen is recorded AFTER the messages commit so a crash mid-file
    # leaves the file un-recorded and the next run reingests it.
    store.record_file_seen(str(path), mtime_unix, size_bytes)
    stats.files_ingested = 1
    stats.messages_skipped_no_text = raw_stats.no_text_count
    stats.messages_skipped_malformed = raw_stats.malformed_count
    return stats


def ingest_directory(root: Path, store: SessionStore, *, force: bool = False) -> IngestStats:
    """Ingest every .jsonl under root (typically ``~/.claude/projects``)."""
    root = Path(root)
    total = IngestStats()
    for jsonl in sorted(root.glob("**/*.jsonl")):
        file_stats = ingest_file(jsonl, store, force=force)
        total.files_seen += file_stats.files_seen
        total.files_ingested += file_stats.files_ingested
        total.files_skipped_unchanged += file_stats.files_skipped_unchanged
        total.messages_inserted += file_stats.messages_inserted
        total.messages_skipped_dupe += file_stats.messages_skipped_dupe
        total.messages_skipped_malformed += file_stats.messages_skipped_malformed
        total.messages_skipped_no_text += file_stats.messages_skipped_no_text
    return total
